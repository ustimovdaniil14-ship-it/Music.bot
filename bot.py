import os
import re
import glob
import html
import uuid
import asyncio
import subprocess
import logging
import sys
import json
from collections import OrderedDict
from urllib.parse import quote


def _ensure_latest_ytdlp():
    """SoundCloud/YouTube постоянно меняют API, старый yt-dlp перестаёт качать.
    При каждом старте обновляем yt-dlp (отключить: AUTO_UPDATE_YTDLP=0)."""
    if os.getenv("AUTO_UPDATE_YTDLP", "1") == "0":
        return
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--quiet",
             "--disable-pip-version-check", "yt-dlp"],
            timeout=180, check=False,
        )
    except Exception as exc:
        print(f"yt-dlp auto-update skipped: {exc}", flush=True)


_ensure_latest_ytdlp()

import yt_dlp  # noqa: E402
from aiohttp import web, ClientSession, ClientTimeout, FormData  # noqa: E402
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================= Настройки =================
# Лучше задать BOT_TOKEN в Environment на Render, а не хранить в коде.
BOT_TOKEN = os.getenv("BOT_TOKEN", "1780243804:04JcO4bUrX_PxIPNsvwDYr8rOoW5hJQ4kNn")
ADMIN_ID = 1780243247
ADMIN_USERNAME = "@PavelSupport"
CHANNEL_USERNAME = "@PavelBet"
PORT = int(os.getenv("PORT", "10000"))
CUSTOM_API_SERVER = "https://bot.catup.lol/"

MAX_RESULTS = 5
MAX_DURATION_SEC = 20 * 60  # не качаем треки длиннее 20 минут
DOWNLOAD_DIR = "downloads"

# Публичные зеркала (меняются со временем — бот пробует по очереди)
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.private.coffee",
    "https://pipedapi.leptons.xyz",
]
INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
]
# =============================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("music_bot")

session = AiohttpSession(api=TelegramAPIServer.from_base(CUSTOM_API_SERVER), timeout=300)
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

HTTP_TIMEOUT = ClientTimeout(total=8)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


class DonateForm(StatesGroup):
    amount = State()


# ---------- Кэш результатов (callback_data ограничена 64 байтами) ----------
RESULTS_CACHE: "OrderedDict[str, list]" = OrderedDict()
FILE_ID_CACHE: dict = {}  # уже отправленные треки -> мгновенная повторная отправка
DL_SEM = asyncio.Semaphore(3)
CONFIRMED_USERS: set = set()  # кто нажал «Я подписался» (сбрасывается при перезапуске)


def cache_results(results: list) -> str:
    token = uuid.uuid4().hex[:8]
    RESULTS_CACHE[token] = results
    while len(RESULTS_CACHE) > 300:
        RESULTS_CACHE.popitem(last=False)
    return token


# ---------- Утилиты ----------
def esc(text) -> str:
    return html.escape(str(text or ""))


def fmt_duration(sec) -> str:
    try:
        m, s = divmod(int(sec or 0), 60)
    except (TypeError, ValueError):
        m, s = 0, 0
    return f"{m}:{s:02d}"


# ---------- Web-сервер для Render ----------
async def handle_ping(request):
    return web.Response(text="OK")


PUBLIC_BASE = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")


async def handle_file(request):
    """Отдаёт скачанный трек по HTTP — Telegram-сервер сам заберёт его по ссылке."""
    name = os.path.basename(request.match_info["name"])
    full = os.path.join(DOWNLOAD_DIR, name)
    if not re.fullmatch(r"[\w.\-]+", name) or not os.path.isfile(full):
        raise web.HTTPNotFound()
    ext = os.path.splitext(name)[1] or ".mp3"
    title = re.sub(r'[\\/:*?"<>|]', "", request.query.get("t", "track")).strip() or "track"
    resp = web.FileResponse(full)
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(title + ext)}"
    return resp


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/dl/{name}", handle_file)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Web server started on port %s", PORT)


# ---------- Подписка ----------
async def check_subscription(user_id: int) -> bool:
    # Мягкий режим: реальную подписку не проверяем, достаточно нажать «✅ Я подписался»
    return user_id in CONFIRMED_USERS


def sub_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")
    b.button(text="✅ Я подписался", callback_data="check_sub")
    b.adjust(1)
    return b.as_markup()


def donate_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="💖 Поддержать автора", callback_data="start_donate")
    return b.as_markup()


# ============================================================
#                         ПОИСК
# ============================================================
# Порядок: SoundCloud (через yt-dlp scsearch — обычно не блокирует
# дата-центры) -> Piped API -> Invidious API -> ytsearch (последний шанс).

def _sc_search_sync(query: str) -> list:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 1,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"scsearch{MAX_RESULTS}:{query}", download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        url = e.get("webpage_url") or e.get("url")
        if not url or not url.startswith("http"):
            continue
        out.append({
            "src": "sc",
            "id": url,
            "title": e.get("title") or "Unknown",
            "artist": e.get("uploader") or "",
            "duration": e.get("duration") or 0,
        })
    return out


async def search_soundcloud(query: str) -> list:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_sc_search_sync, query), timeout=25)
    except Exception as e:
        log.warning("SoundCloud search failed: %s", e)
        return []


async def search_piped(query: str) -> list:
    async with ClientSession(timeout=HTTP_TIMEOUT, headers=UA) as http:
        for base in PIPED_INSTANCES:
            for flt in ("music_songs", "videos"):
                try:
                    async with http.get(f"{base}/search", params={"q": query, "filter": flt}) as r:
                        if r.status != 200:
                            continue
                        data = await r.json(content_type=None)
                    out = []
                    for it in data.get("items", []):
                        m = re.search(r"v=([\w-]{11})", it.get("url", ""))
                        if not m:
                            continue
                        dur = it.get("duration") or 0
                        if dur < 0:
                            continue
                        out.append({
                            "src": "yt",
                            "id": m.group(1),
                            "title": it.get("title") or "Unknown",
                            "artist": it.get("uploaderName") or "",
                            "duration": dur,
                        })
                        if len(out) >= MAX_RESULTS:
                            break
                    if out:
                        return out
                except Exception as e:
                    log.warning("Piped %s failed: %s", base, e)
    return []


async def search_invidious(query: str) -> list:
    async with ClientSession(timeout=HTTP_TIMEOUT, headers=UA) as http:
        for base in INVIDIOUS_INSTANCES:
            try:
                async with http.get(f"{base}/api/v1/search", params={"q": query, "type": "video"}) as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
                out = []
                for it in data:
                    if it.get("type", "video") != "video" or not it.get("videoId"):
                        continue
                    out.append({
                        "src": "yt",
                        "id": it["videoId"],
                        "title": it.get("title") or "Unknown",
                        "artist": it.get("author") or "",
                        "duration": it.get("lengthSeconds") or 0,
                    })
                    if len(out) >= MAX_RESULTS:
                        break
                if out:
                    return out
            except Exception as e:
                log.warning("Invidious %s failed: %s", base, e)
    return []


def _yt_search_sync(query: str) -> list:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True, "socket_timeout": 10, "retries": 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{MAX_RESULTS}:{query}", download=False)
    return [{
        "src": "yt", "id": e["id"], "title": e.get("title") or "Unknown",
        "artist": e.get("uploader") or "", "duration": e.get("duration") or 0,
    } for e in (info or {}).get("entries") or [] if e and e.get("id")]


async def search_ytdlp_youtube(query: str) -> list:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_yt_search_sync, query), timeout=25)
    except Exception as e:
        log.warning("yt-dlp ytsearch failed: %s", e)
        return []


async def find_tracks(query: str) -> list:
    for fn in (search_soundcloud, search_piped, search_invidious, search_ytdlp_youtube):
        res = await fn(query)
        if res:
            log.info("Search '%s' served by %s (%d results)", query, fn.__name__, len(res))
            return res[:MAX_RESULTS]
    return []


# ============================================================
#                        СКАЧИВАНИЕ
# ============================================================
def _find_audio(prefix: str):
    for f in glob.glob(f"{prefix}*"):
        if f.endswith((".part", ".ytdl", ".raw", ".tmp")):
            continue
        return f
    return None


YDL_FORMATS = [
    "http_mp3_128/bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
    "best",
    None,  # формат по умолчанию yt-dlp
]


def _ytdlp_download_sync(url: str, prefix: str):
    # Без перекодирования: Telegram принимает mp3 и m4a как есть -> быстро.
    path = None
    last_err = None
    for fmt in YDL_FORMATS:
        for leftover in glob.glob(f"{prefix}*"):
            try:
                os.remove(leftover)
            except Exception:
                pass
        opts = {
            "outtmpl": f"{prefix}.%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "socket_timeout": 10,
            "retries": 2,
            "fragment_retries": 2,
            "concurrent_fragment_downloads": 8,
        }
        if fmt:
            opts["format"] = fmt
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
            path = _find_audio(prefix)
            if path:
                break
        except Exception as e:
            last_err = e
            log.warning("yt-dlp attempt (format=%s) failed: %s", fmt, e)
    if not path and last_err:
        raise last_err
    log.info("yt-dlp %s downloaded: %s", getattr(yt_dlp.version, "__version__", "?"), path)
    if path and not path.endswith((".mp3", ".m4a")):
        dst = f"{prefix}_conv.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-vn", "-b:a", "160k", dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        try:
            os.remove(path)
        except Exception:
            pass
        path = dst if os.path.exists(dst) else None
    return path


async def _ffmpeg_to_mp3(src: str, dst: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", src, "-vn", "-acodec", "libmp3lame", "-b:a", "192k", dst,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0 and os.path.exists(dst)


async def _get_audio_stream_urls(video_id: str) -> list:
    """Возвращает список прямых ссылок на аудио-поток через Piped/Invidious."""
    urls = []
    async with ClientSession(timeout=HTTP_TIMEOUT, headers=UA) as http:
        for base in PIPED_INSTANCES:
            try:
                async with http.get(f"{base}/streams/{video_id}") as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
                streams = sorted(data.get("audioStreams", []),
                                 key=lambda s: s.get("bitrate", 0), reverse=True)
                urls += [s["url"] for s in streams if s.get("url")][:2]
            except Exception as e:
                log.warning("Piped streams %s failed: %s", base, e)
            if urls:
                return urls
        for base in INVIDIOUS_INSTANCES:
            try:
                async with http.get(f"{base}/api/v1/videos/{video_id}", params={"local": "true"}) as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
                fmts = [f for f in data.get("adaptiveFormats", [])
                        if str(f.get("type", "")).startswith("audio") and f.get("url")]
                fmts.sort(key=lambda f: int(f.get("bitrate", 0) or 0), reverse=True)
                urls += [f["url"] for f in fmts][:2]
            except Exception as e:
                log.warning("Invidious streams %s failed: %s", base, e)
            if urls:
                return urls
    return urls


async def _download_via_stream(video_id: str, prefix: str):
    stream_urls = await _get_audio_stream_urls(video_id)
    raw = f"{prefix}.raw"
    dst = f"{prefix}.mp3"
    for u in stream_urls:
        try:
            async with ClientSession(timeout=ClientTimeout(total=180), headers=UA) as http:
                async with http.get(u) as r:
                    if r.status != 200:
                        continue
                    with open(raw, "wb") as f:
                        async for chunk in r.content.iter_chunked(256 * 1024):
                            f.write(chunk)
            if os.path.getsize(raw) > 10_000 and await _ffmpeg_to_mp3(raw, dst):
                return dst
        except Exception as e:
            log.warning("Stream download failed: %s", e)
        finally:
            if os.path.exists(raw):
                os.remove(raw)
    return None


async def download_track(item: dict, prefix: str):
    """Возвращает путь к mp3 или None."""
    if item["src"] == "sc":
        return await asyncio.to_thread(_ytdlp_download_sync, item["id"], prefix)

    # YouTube: сначала через зеркала (поток), потом напрямую yt-dlp
    path = await _download_via_stream(item["id"], prefix)
    if path:
        return path
    try:
        return await asyncio.to_thread(
            _ytdlp_download_sync, f"https://www.youtube.com/watch?v={item['id']}", prefix)
    except Exception as e:
        log.warning("yt-dlp YouTube download failed: %s", e)
        return None


# ============================================================
#                        ХЭНДЛЕРЫ
# ============================================================
def sub_text() -> str:
    return (
        "👋 <b>Привет! Добро пожаловать в Music Bot</b>\n\n"
        "🎧 Я ищу и присылаю любые треки в MP3 прямо в чат — быстро и бесплатно.\n\n"
        "🔒 <b>Чтобы пользоваться ботом, нужно подписаться на наш канал:</b>\n"
        f"📢 <b>{esc(CHANNEL_USERNAME)}</b>\n\n"
        "Там мы публикуем новости, обновления бота и музыку.\n\n"
        "<b>Что делать:</b>\n"
        "1️⃣ Нажми «📢 Подписаться на канал»\n"
        "2️⃣ Вернись сюда и нажми «✅ Я подписался»\n"
        "3️⃣ Пользуйся ботом 🎶"
    )


def welcome_text(first: bool = True) -> str:
    head = ("🎉 <b>Подписка подтверждена! Спасибо!</b>\n\n" if first
            else "🎧 <b>Добро пожаловать в Music Bot!</b>\n\n")
    return (
        head
        + "Я найду и пришлю любой трек в формате MP3 за пару секунд.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1️⃣ Напиши название песни или имя исполнителя\n"
        "2️⃣ Выбери нужный трек из списка\n"
        "3️⃣ Получи аудиофайл прямо в чат\n\n"
        "💡 <b>Советы:</b>\n"
        "• Лучше писать «исполнитель — название»\n"
        "• Можно искать на русском и английском\n"
        "• Если трек не нашёлся — попробуй другой вариант написания\n\n"
        "💖 Бот бесплатный, а поддержать автора можно кнопкой ниже.\n\n"
        "🎵 <b>Введи название песни или имя исполнителя...</b>"
    )


HELP_TEXT = (
    "ℹ️ <b>Справка</b>\n\n"
    "Просто отправь мне название песни или имя исполнителя — я покажу 5 лучших "
    "совпадений. Нажми на нужный трек, и я пришлю MP3.\n\n"
    "<b>Команды:</b>\n"
    "/start — главное меню\n"
    "/help — эта справка\n\n"
    f"📢 Канал: {esc(CHANNEL_USERNAME)}\n"
    f"💬 Поддержка и вопросы: {esc(ADMIN_USERNAME)}"
)


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if not await check_subscription(message.from_user.id):
        await message.answer(sub_text(), reply_markup=sub_keyboard())
    else:
        await message.answer(welcome_text(first=False), reply_markup=donate_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, reply_markup=donate_keyboard())


@dp.message(Command("probe"))
async def cmd_probe(message: types.Message):
    """Только для админа: проверяет, файлы какого размера принимает сервер."""
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🧪 Проверяю, какие размеры файлов принимает сервер...")
    lines = []
    for kb, sec in ((100, 6), (500, 31), (1000, 62), (2000, 125), (4000, 250)):
        p = os.path.join(DOWNLOAD_DIR, f"probe_{kb}.mp3")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", str(sec), "-b:a", "128k", p,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=60)
            size = os.path.getsize(p)
            await send_audio_direct(message.chat.id, p, {"title": f"probe {kb} KB"}, None)
            lines.append(f"✅ {size // 1024} КБ — принят")
        except Exception as e:
            lines.append(f"❌ ~{kb} КБ — {str(e)[:120]}")
        finally:
            try:
                os.remove(p)
            except Exception:
                pass
    await message.answer("<b>Итог:</b>\n" + "\n".join(lines))


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: types.CallbackQuery):
    CONFIRMED_USERS.add(callback.from_user.id)
    try:
        await callback.message.edit_text(welcome_text(first=True), reply_markup=donate_keyboard())
    except Exception:
        pass
    await callback.answer()


# ---------- Донаты ----------
@dp.callback_query(F.data == "start_donate")
async def cb_start_donate(callback: types.CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="⭐️ Звёздами (Telegram Stars)", callback_data="donate_type_stars")
    b.button(text="🖼 Подарить NFT", callback_data="donate_type_nft")
    b.adjust(1)
    await callback.message.answer("💖 <b>Выбери способ поддержки:</b>", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "donate_type_nft")
async def cb_donate_nft(callback: types.CallbackQuery):
    await callback.message.answer(
        f"🖼 <b>Спасибо за поддержку!</b>\nОтправить NFT можно админу: {esc(ADMIN_USERNAME)}")
    await callback.answer()


@dp.callback_query(F.data == "donate_type_stars")
async def cb_donate_stars(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("⭐️ Введи количество звёзд (цифрами):")
    await state.set_state(DonateForm.amount)
    await callback.answer()


@dp.message(DonateForm.amount)
async def donate_amount(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or not (0 < int(text) <= 100000):
        await message.answer("⚠️ Введи корректное число звёзд (например: 50):")
        return
    amount = int(text)
    try:
        await bot.send_invoice(
            chat_id=message.from_user.id,
            title="Поддержка автора 💖",
            description=f"Пожертвование {amount} Telegram Stars.",
            payload=f"music_donate_{message.from_user.id}",
            currency="XTR",
            prices=[types.LabeledPrice(label="Поддержка автора", amount=amount)],
            provider_token="",
        )
    except Exception as e:
        log.error("Invoice error: %s", e)
        await message.answer("⚠️ Не удалось создать счёт. Попробуй позже.")
    await state.clear()


@dp.pre_checkout_query()
async def pre_checkout(q: types.PreCheckoutQuery):
    try:
        await bot.answer_pre_checkout_query(q.id, ok=True)
    except Exception as e:
        log.error("Pre-checkout error: %s", e)


@dp.message(F.successful_payment)
async def payment_success(message: types.Message):
    await message.answer("💖 <b>Спасибо за поддержку!</b>")


# ---------- Поиск ----------
@dp.message(F.text & ~F.text.startswith("/"))
async def on_search(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    query = message.text.strip()
    if not query:
        return

    if not await check_subscription(message.from_user.id):
        await message.answer(sub_text(), reply_markup=sub_keyboard())
        return

    wait = await message.answer("🔍 Ищу трек...")
    try:
        results = await find_tracks(query)
    except Exception as e:
        log.error("Search error: %s", e)
        results = []

    if not results:
        await wait.edit_text("❌ Ничего не найдено. Попробуй ввести «исполнитель — название».")
        return

    token = cache_results(results)
    b = InlineKeyboardBuilder()
    for i, r in enumerate(results):
        label = f"🎵 {r['title'][:38]} ({fmt_duration(r['duration'])})"
        b.button(text=label, callback_data=f"dl:{token}:{i}")
    b.button(text="💖 Поддержать автора", callback_data="start_donate")
    b.adjust(1)

    await wait.edit_text(
        f"🔎 Результаты по запросу: <b>{esc(query)}</b>\nВыбери трек:",
        reply_markup=b.as_markup(),
    )


# ---------- Скачивание ----------
async def to_clean_mp3(path: str, prefix: str) -> str:
    """Перекодирует в стандартный MP3 128k (быстро, ~2–4 с). При сбое возвращает исходный файл."""
    dst = f"{prefix}_final.mp3"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", path, "-vn", "-map_metadata", "-1",
            "-c:a", "libmp3lame", "-b:a", "128k", dst,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=60)
        if proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 10_000:
            if dst != path:
                try:
                    os.remove(path)
                except Exception:
                    pass
            return dst
    except Exception as e:
        log.warning("ffmpeg convert failed: %s", e)
    return path


async def send_audio_direct(chat_id: int, path: str, item: dict, caption: str) -> dict:
    """sendAudio напрямую через multipart: файл лежит в поле `audio` (формат из доков Catugram)."""
    url = f"{CUSTOM_API_SERVER.rstrip('/')}/bot{BOT_TOKEN}/sendAudio"
    fields = {
        "chat_id": str(chat_id),
        "title": item["title"][:100],
        "reply_markup": json.dumps(
            {"inline_keyboard": [[{"text": "💖 Поддержать автора", "callback_data": "start_donate"}]]},
            ensure_ascii=False),
    }
    if caption:
        fields["caption"] = caption
        fields["parse_mode"] = "HTML"
    if item.get("artist"):
        fields["performer"] = item["artist"][:100]
    if item.get("duration"):
        fields["duration"] = str(int(item["duration"]))

    # Собираем multipart вручную: тело целиком в памяти, есть Content-Length (без chunked),
    # как делает curl -F audio=@file — некоторые самописные серверы рвут chunked-загрузки.
    boundary = uuid.uuid4().hex
    chunks = []
    for k, v in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            + v.encode("utf-8") + b"\r\n")
    with open(path, "rb") as fh:
        file_bytes = fh.read()
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="track.mp3"\r\n'
        f'Content-Type: audio/mpeg\r\n\r\n'.encode() + file_bytes + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)

    async with ClientSession(timeout=ClientTimeout(total=180)) as http:
        async with http.post(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        ) as resp:
            raw = await resp.text()
            try:
                data = json.loads(raw)
            except Exception:
                raise RuntimeError(f"HTTP {resp.status}: {raw[:200]}")
    if not data.get("ok"):
        raise RuntimeError(f"{data.get('error_code')} {data.get('description')}")
    return data.get("result") or {}


UPLOAD_FAIL_BYTES = None  # минимальный размер файла, который сервер отверг (FILE_ID_INVALID)


async def _reencode(src: str, dst: str, kbps: int) -> bool:
    args = ["ffmpeg", "-y", "-i", src, "-vn", "-map_metadata", "-1",
            "-c:a", "libmp3lame", "-b:a", f"{kbps}k"]
    if kbps <= 64:
        args += ["-ac", "1"]
    args.append(dst)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=60)
        return proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 5000
    except Exception as e:
        log.warning("reencode failed: %s", e)
        return False


async def send_direct_adaptive(chat_id: int, path: str, item: dict, caption, prefix: str) -> dict:
    """Загрузка файла напрямую. Если сервер отвергает большой файл (FILE_ID_INVALID —
    так ведёт себя обрезанное тело запроса), пробуем меньший битрейт и запоминаем лимит."""
    global UPLOAD_FAIL_BYTES
    dur = item.get("duration") or 180
    kbps = 128
    if UPLOAD_FAIL_BYTES:
        kbps = int(min(128, max(32, UPLOAD_FAIL_BYTES * 0.8 * 8 / 1000 / dur)))
    while True:
        cur = path
        if kbps < 128:
            dst = f"{prefix}_{kbps}k.mp3"
            if await _reencode(path, dst, kbps):
                cur = dst
        size = os.path.getsize(cur)
        try:
            res = await send_audio_direct(chat_id, cur, item, caption)
            log.info("direct upload ok: %d bytes @ %dk", size, kbps)
            if UPLOAD_FAIL_BYTES and size >= UPLOAD_FAIL_BYTES:
                UPLOAD_FAIL_BYTES = None  # лимит оказался ложным
            return res
        except Exception as e:
            if "FILE_ID_INVALID" not in str(e) or kbps <= 32:
                raise
            UPLOAD_FAIL_BYTES = size if UPLOAD_FAIL_BYTES is None else min(UPLOAD_FAIL_BYTES, size)
            log.warning("direct upload rejected at %d bytes (%dk), retrying smaller", size, kbps)
            kbps = max(32, kbps // 2)


async def _delayed_remove(path: str, delay: int):
    await asyncio.sleep(delay)
    try:
        os.remove(path)
    except Exception:
        pass


_LAST_NOTIFY = 0.0


async def notify_admin(text: str):
    """Присылает админу подробности ошибки (не чаще раза в 5 минут)."""
    global _LAST_NOTIFY
    now = asyncio.get_running_loop().time()
    if now - _LAST_NOTIFY < 30:
        return
    _LAST_NOTIFY = now
    try:
        await bot.send_message(ADMIN_ID, f"🛠 <pre>{esc(text[:3500])}</pre>")
    except Exception as e:
        log.warning("notify_admin failed: %s", e)


@dp.callback_query(F.data.startswith("dl:"))
async def cb_download(callback: types.CallbackQuery):
    try:
        _, token, idx = callback.data.split(":")
        item = RESULTS_CACHE[token][int(idx)]
    except Exception:
        await callback.answer("⌛ Результаты устарели, повтори поиск.", show_alert=True)
        return

    if item["duration"] and item["duration"] > MAX_DURATION_SEC:
        await callback.answer("⚠️ Трек слишком длинный (максимум 20 минут).", show_alert=True)
        return

    await callback.answer()
    key = f"{item['src']}:{item['id']}"
    caption = None  # без подписи: приходит только трек с кнопкой «Поддержать автора»
    cached = FILE_ID_CACHE.get(key)
    if cached:
        try:
            await callback.message.answer_audio(
                audio=cached, caption=caption, reply_markup=donate_keyboard())
            return
        except Exception as e:
            log.warning("Cached file_id failed: %s", e)

    status = await callback.message.answer(f"📥 Скачиваю: {esc(item['title'][:60])}...")
    prefix = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex)
    path = None
    keep_file = False
    try:
        async with DL_SEM:
            path = await asyncio.wait_for(download_track(item, prefix), timeout=90)
        log.info("Ready to send: %s", path)
        if not path or not os.path.exists(path):
            await status.edit_text("❌ Не удалось скачать этот трек. Попробуй другой вариант.")
            return

        # Всегда делаем чистый MP3 (ровные заголовки) — приватный сервер может
        # отвергать «сырые» файлы (MEDIA_INVALID).
        path = await to_clean_mp3(path, prefix)
        log.info("Sending %s (%d bytes)", path, os.path.getsize(path))
        chat_id = callback.message.chat.id

        async def _via_direct():
            # Как в документации Catugram: файл прямо в поле audio (без attach://)
            res = await send_direct_adaptive(chat_id, path, item, caption, prefix)
            return (res.get("audio") or {}).get("file_id")

        async def _via_url():
            msg = await callback.message.answer_audio(
                audio=f"{PUBLIC_BASE}/dl/{os.path.basename(path)}",
                title=item["title"][:100],
                performer=(item.get("artist") or "")[:100] or None,
                duration=int(item["duration"]) if item.get("duration") else None,
                caption=caption,
                reply_markup=donate_keyboard(),
            )
            return msg.audio.file_id if msg.audio else None

        async def _via_aiogram():
            msg = await callback.message.answer_audio(
                audio=types.FSInputFile(path),
                title=item["title"][:100],
                performer=(item.get("artist") or "")[:100] or None,
                duration=int(item["duration"]) if item.get("duration") else None,
                caption=caption,
                reply_markup=donate_keyboard(),
            )
            return msg.audio.file_id if msg.audio else None

        methods = [("direct-upload", _via_direct)]
        if PUBLIC_BASE:
            methods.append(("url", _via_url))
        methods.append(("aiogram-upload", _via_aiogram))

        sent = None
        file_id = None
        errors = []
        for method, fn in methods:
            try:
                file_id = await fn()
                sent = "ok"
                log.info("Sent via %s", method)
                break
            except Exception as send_err:
                log.warning("send via %s failed: %s", method, send_err)
                errors.append(f"{method}: {type(send_err).__name__}: {send_err}")
        if sent is None and PUBLIC_BASE:
            # Приватный сервер не принимает файлы -> даём прямую ссылку на MP3
            link = f"{PUBLIC_BASE}/dl/{os.path.basename(path)}?t={quote(item['title'][:80])}"
            lb = InlineKeyboardBuilder()
            lb.button(text="⬇️ Скачать MP3", url=link)
            lb.button(text="💖 Поддержать автора", callback_data="start_donate")
            lb.adjust(1)
            await callback.message.answer(
                f"🎧 <b>{esc(item['title'])}</b>\n"
                f"⏱ {fmt_duration(item['duration'])}\n\n"
                "Трек готов! Нажми кнопку ниже — файл скачается прямо на телефон.\n"
                "⏳ Ссылка действует 30 минут.\n\n"
                f"📢 {esc(CHANNEL_USERNAME)}",
                reply_markup=lb.as_markup(),
            )
            keep_file = True
            asyncio.create_task(_delayed_remove(path, 1800))
            sent = "link"
        if errors:
            await notify_admin("Отправка трека (использована ссылка):\n" + "\n".join(errors))
        if sent is None:
            raise RuntimeError("не удалось отправить файл (PUBLIC_URL не задан)")
        if sent == "ok" and file_id:
            FILE_ID_CACHE[key] = file_id
        try:
            await status.delete()
        except Exception:
            pass
    except asyncio.TimeoutError:
        await status.edit_text("⌛ Загрузка заняла слишком много времени. Попробуй другой трек.")
    except Exception as e:
        log.exception("Download error")
        reason = f"{type(e).__name__}: {e}"
        await notify_admin(f"Ошибка скачивания «{item['title'][:60]}» ({item['src']}):\n{reason}")
        text = "⚠️ Не удалось скачать этот трек. Попробуй другой вариант из списка."
        if callback.from_user.id == ADMIN_ID:
            text += f"\n\n<code>{esc(reason[:300])}</code>"
        try:
            await status.edit_text(text)
        except Exception:
            pass
    finally:
        if not keep_file:
            for f in glob.glob(f"{prefix}*"):
                try:
                    os.remove(f)
                except Exception:
                    pass


# ---------- Запуск ----------
async def set_main_menu():
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню / Поиск музыки"),
            BotCommand(command="help", description="Справка"),
        ])
    except Exception as e:
        log.warning("Commands error: %s", e)


async def main():
    await start_web_server()
    await set_main_menu()
    log.info("🎵 Музыкальный бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
