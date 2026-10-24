import os
import re
import glob
import html
import uuid
import asyncio
import logging
from collections import OrderedDict

import yt_dlp
from aiohttp import web, ClientSession, ClientTimeout
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

session = AiohttpSession(api=TelegramAPIServer.from_base(CUSTOM_API_SERVER))
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


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Web server started on port %s", PORT)


# ---------- Подписка ----------
async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception as e:
        # Если бот не админ канала — не блокируем пользователей
        log.warning("Sub check failed: %s", e)
        return True


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
def _find_mp3(prefix: str):
    files = glob.glob(f"{prefix}*.mp3")
    return files[0] if files else None


def _ytdlp_download_sync(url: str, prefix: str):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{prefix}.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": 15,
        "retries": 2,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    return _find_mp3(prefix)


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
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if not await check_subscription(message.from_user.id):
        await message.answer(
            f"👋 <b>Привет!</b>\n\nДля доступа к поиску музыки подпишись на канал "
            f"<b>{esc(CHANNEL_USERNAME)}</b> и нажми «✅ Я подписался» 👇",
            reply_markup=sub_keyboard(),
        )
    else:
        await message.answer(
            "🎵 Введи название песни или имя исполнителя...",
            reply_markup=donate_keyboard(),
        )


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: types.CallbackQuery):
    if await check_subscription(callback.from_user.id):
        try:
            await callback.message.edit_text(
                "🎉 <b>Подписка подтверждена!</b>\n\n🎵 Введи название песни или имя исполнителя...",
                reply_markup=donate_keyboard(),
            )
        except Exception:
            pass
        await callback.answer()
    else:
        await callback.answer("❌ Ты всё ещё не подписан на канал!", show_alert=True)


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
        await message.answer(
            f"⚠️ Для поиска музыки подпишись на канал: {esc(CHANNEL_USERNAME)}",
            reply_markup=sub_keyboard(),
        )
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
    status = await callback.message.answer(f"📥 Скачиваю: {esc(item['title'][:60])}...")
    prefix = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex)
    path = None
    try:
        path = await asyncio.wait_for(download_track(item, prefix), timeout=240)
        if not path or not os.path.exists(path):
            await status.edit_text("❌ Не удалось скачать этот трек. Попробуй другой вариант.")
            return

        await callback.message.answer_audio(
            audio=types.FSInputFile(path),
            title=item["title"][:100],
            performer=(item.get("artist") or "")[:100] or None,
            duration=int(item["duration"]) if item.get("duration") else None,
            caption=f"🎧 <b>{esc(item['title'])}</b>\n\n📢 {esc(CHANNEL_USERNAME)}",
            reply_markup=donate_keyboard(),
        )
        try:
            await status.delete()
        except Exception:
            pass
    except asyncio.TimeoutError:
        await status.edit_text("⌛ Загрузка заняла слишком много времени. Попробуй другой трек.")
    except Exception as e:
        log.error("Download error: %s", e)
        try:
            await status.edit_text("⚠️ Не удалось скачать этот трек.")
        except Exception:
            pass
    finally:
        for f in glob.glob(f"{prefix}*"):
            try:
                os.remove(f)
            except Exception:
                pass


# ---------- Запуск ----------
async def set_main_menu():
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Главное меню / Поиск музыки")])
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
