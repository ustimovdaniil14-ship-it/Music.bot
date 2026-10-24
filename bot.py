import os
import asyncio
import logging
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
import yt_dlp

# ================= Настройки =================
BOT_TOKEN = "1780243804:04JcO4bUrX_PxIPNsvwDYr8rOoW5hJQ4kNn"
ADMIN_ID = 1780243247
ADMIN_USERNAME = "@PavelSupport"
CHANNEL_USERNAME = "@PavelBet"  # Твой канал
PORT = int(os.getenv("PORT", "10000"))

CUSTOM_API_SERVER = "https://bot.catup.lol/"
# =============================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("music_bot")

session = AiohttpSession(api=TelegramAPIServer.from_base(CUSTOM_API_SERVER))
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

class DonateForm(StatesGroup):
    amount = State()

YDL_DOWNLOAD_OPTIONS = {
    'format': 'bestaudio/best',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    'outtmpl': 'downloads/%(id)s.%(ext)s',
    'quiet': True,
    'nocheckcertificate': True,
}

os.makedirs('downloads', exist_ok=True)

# --- Проверка подписки на канал ---
async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ["creator", "administrator", "member"]
    except Exception as e:
        log.warning(f"Sub check failed: {e}")
        # Если бот не админ в канале, пропускаем проверку
        return True

async def handle_ping(request):
    return web.Response(text="Music Bot is alive!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

async def set_main_menu(bot: Bot):
    try:
        commands = [BotCommand(command="start", description="Главное меню / Поиск музыки")]
        await bot.set_my_commands(commands)
    except Exception as e:
        log.warning(f"Commands error: {e}")

def get_sub_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")
    builder.button(text="✅ Проверить подписку", callback_data="check_sub")
    builder.adjust(1)
    return builder

def get_donate_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💖 Поддержать автора", callback_data="start_donate")
    return builder

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    is_subbed = await check_subscription(message.from_user.id)
    
    if not is_subbed:
        builder = get_sub_keyboard()
        await message.answer(
            f"👋 **Привет!**\n\n"
            f"Для доступа к поиску музыки обязательно подпишись на наш канал: **{CHANNEL_USERNAME}**.\n"
            f"После подписки нажми кнопку ниже 👇",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    else:
        builder = get_donate_keyboard()
        await message.answer(
            f"🚀 **Привет! Я готов искать музыку.**\n\n"
            f"Просто напиши название песни или имя исполнителя в чат (например: `Scally Milano` или `Kizaru`):",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )

@dp.callback_query(F.data == "check_sub")
async def process_check_sub(callback: types.CallbackQuery):
    is_subbed = await check_subscription(callback.from_user.id)
    if is_subbed:
        builder = get_donate_keyboard()
        await callback.message.edit_text(
            f"🎉 **Отлично! Подписка подтверждена.**\n\n"
            f"🎵 Введи название песни или исполнителя прямо в чат 👇",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    else:
        await callback.answer("❌ Вы всё ещё не подписались на канал!", show_alert=True)

@dp.callback_query(F.data == "start_donate")
async def process_start_donate(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐️ Звёздами (Telegram Stars)", callback_data="donate_type_stars")
    builder.button(text="🖼 Подарить NFT", callback_data="donate_type_nft")
    builder.adjust(1)
    await callback.message.answer("💖 **Выберите способ поддержки:**", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "donate_type_nft")
async def process_donate_nft(callback: types.CallbackQuery):
    await callback.message.answer(f"🖼 **Спасибо за поддержку!**\nОтправить NFT можно админу: {ADMIN_USERNAME}")
    await callback.answer()

@dp.callback_query(F.data == "donate_type_stars")
async def process_donate_stars(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("⭐️ Введите количество звёзд (цифрами):")
    await state.set_state(DonateForm.amount)
    await callback.answer()

@dp.message(DonateForm.amount)
async def process_donate_amount(message: types.Message, state: FSMContext):
    text_input = message.text.strip() if message.text else ""
    if not text_input.isdigit() or int(text_input) <= 0:
        await message.answer("⚠️ Введите корректное число звёзд:")
        return

    amount = int(text_input)
    prices = [types.LabeledPrice(label="Поддержка автора", amount=amount)]
    await bot.send_invoice(
        chat_id=message.from_user.id,
        title="Поддержка автора 💖",
        description=f"Пожертвование {amount} Telegram Stars.",
        payload=f"music_donate_{message.from_user.id}",
        currency="XTR",
        prices=prices,
        provider_token=""
    )
    await state.clear()

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def payment_success(message: types.Message):
    await message.answer("💖 **Спасибо за поддержку!**")

# --- Поиск треков через инстансы Invidious API (обход блокировок Render) ---
async def fetch_music_results(query: str):
    api_urls = [
        f"https://invidious.nerdvpn.de/api/v1/search?q={query}&type=video",
        f"https://vid.pugilist.kavin.rocks/api/v1/search?q={query}&type=video",
        f"https://inv.riverside.rocks/api/v1/search?q={query}&type=video"
    ]
    async with ClientSession() as session_http:
        for url in api_urls:
            try:
                async with session_http.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = []
                        for item in data[:5]:
                            results.append({
                                'id': item.get('videoId'),
                                'title': item.get('title'),
                                'duration': item.get('lengthSeconds', 0)
                            })
                        if results:
                            return results
            except Exception:
                continue
    return []

@dp.message()
async def search_music(message: types.Message):
    query = message.text.strip()
    if not query or query.startswith("/"):
        return

    # Проверка подписки перед каждым поиском
    is_subbed = await check_subscription(message.from_user.id)
    if not is_subbed:
        builder = get_sub_keyboard()
        await message.answer(f"⚠️ Для поиска музыки подпишитесь на канал: {CHANNEL_USERNAME}", reply_markup=builder.as_markup())
        return

    wait_msg = await message.answer("🔍 Ищу трек в базе...")

    try:
        entries = await fetch_music_results(query)

        if not entries:
            # Запасной вариант через yt-dlp напрямую
            def fallback_search():
                opts = {'format': 'bestaudio/best', 'default_search': 'ytsearch5', 'quiet': True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(query, download=False)
                    return info.get('entries', [])
            
            raw_entries = await asyncio.to_thread(fallback_search)
            entries = []
            for e in raw_entries[:5]:
                if e:
                    entries.append({'id': e.get('id'), 'title': e.get('title'), 'duration': e.get('duration', 0)})

        if not entries:
            await wait_msg.edit_text("❌ Трек не найден. Попробуй ввести название и исполнителя вместе.")
            return

        builder = InlineKeyboardBuilder()
        for entry in entries:
            title = entry.get('title', 'Unknown')
            dur_sec = entry.get('duration', 0)
            mins, secs = divmod(int(dur_sec or 0), 60)
            duration_str = f"{mins}:{secs:02d}"
            v_id = entry.get('id')
            if v_id:
                builder.button(text=f"🎵 {title[:32]} ({duration_str})", callback_data=f"dl_{v_id}")

        builder.button(text="💖 Поддержать автора", callback_data="start_donate")
        builder.adjust(1)

        await wait_msg.edit_text(
            f"🔎 Результаты по запросу: **{query}**\nВыбери трек для скачивания:",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )

    except Exception as e:
        log.error(f"Search error: {e}")
        await wait_msg.edit_text("⚠️ Ошибка при поиске. Попробуй изменить запрос.")

@dp.callback_query(F.data.startswith("dl_"))
async def download_track(callback: types.CallbackQuery):
    video_id = callback.data.split("_", 1)[1]
    url = f"https://www.youtube.com/watch?v={video_id}"

    await callback.message.edit_text("📥 Скачиваю и подготавливаю MP3...")

    file_path = None
    try:
        def run_download():
            with yt_dlp.YoutubeDL(YDL_DOWNLOAD_OPTIONS) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                base, _ = os.path.splitext(filename)
                return f"{base}.mp3", info.get('title', 'music')

        file_path, title = await asyncio.to_thread(run_download)

        if os.path.exists(file_path):
            audio_file = types.FSInputFile(file_path)
            builder = get_donate_keyboard()
            await callback.message.answer_audio(
                audio=audio_file,
                caption=f"🎧 `{title}`\n\n📢 Канал автора: {CHANNEL_USERNAME}",
                reply_markup=builder.as_markup(),
                parse_mode="Markdown"
            )
            await callback.message.delete()
        else:
            await callback.message.edit_text("❌ Ошибка загрузки файла.")

    except Exception as e:
        log.error(f"Download error: {e}")
        await callback.message.edit_text("⚠️ Не удалось скачать этот трек.")
    
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

async def main():
    await start_dummy_server()
    await set_main_menu(bot)
    log.info("🎵 Музыкальный бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
