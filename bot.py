import os
import asyncio
import logging
from aiohttp import web
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
CHANNEL_USERNAME = "@PavelBet"
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

# Надежные настройки yt-dlp для обхода ограничений хостинга
YDL_SEARCH_OPTIONS = {
    'format': 'bestaudio/best',
    'default_search': 'ytsearch5',
    'noplaylist': True,
    'quiet': True,
    'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
}

YDL_DOWNLOAD_OPTIONS = {
    'format': 'bestaudio/best',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    'outtmpl': 'downloads/%(id)s.%(ext)s',
    'quiet': True,
    'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
}

os.makedirs('downloads', exist_ok=True)

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
        commands = [
            BotCommand(command="start", description="Главное меню / Поиск музыки"),
        ]
        await bot.set_my_commands(commands)
    except Exception as e:
        log.warning(f"Could not set commands: {e}")

def get_welcome_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")
    builder.button(text="✅ Я подписался", callback_data="check_sub")
    builder.button(text="💖 Поддержать автора", callback_data="start_donate")
    builder.adjust(1)
    return builder

def get_ready_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💖 Поддержать автора", callback_data="start_donate")
    return builder

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    builder = get_welcome_keyboard()
    
    text = (
        f"👋 **Добро пожаловать в лучший музыкальный бот!**\n\n"
        f"📌 Чтобы получить полный доступ к поиску и скачиванию треков, пожалуйста, подпишись на наш официальный канал: **{CHANNEL_USERNAME}**.\n\n"
        f"После подписки нажми кнопку **«✅ Я подписался»** ниже 👇"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "check_sub")
async def process_check_sub(callback: types.CallbackQuery):
    builder = get_ready_keyboard()
    await callback.message.edit_text(
        f"🎉 **Отлично! Спасибо за подписку на {CHANNEL_USERNAME}!**\n\n"
        f"🎵 **Теперь всё готово.** Просто отправь мне в чат название песни или имя исполнителя (например, *Scally Milano* или *Kizaru*), и я мгновенно найду музыку для тебя! 👇",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer("Подтверждено! Приятного прослушивания 🎧")

@dp.callback_query(F.data == "start_donate")
async def process_start_donate(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐️ Звёздами (Telegram Stars)", callback_data="donate_type_stars")
    builder.button(text="🖼 Подарить NFT", callback_data="donate_type_nft")
    builder.adjust(1)

    await callback.message.answer("💖 **Выберите удобный способ поддержки автора:**", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "donate_type_nft")
async def process_donate_nft(callback: types.CallbackQuery):
    await callback.message.answer(
        f"🖼 **Спасибо за вашу щедрость!**\n\n"
        f"Вы можете отправить ваш подарок или NFT организатору: {ADMIN_USERNAME}"
    )
    await callback.answer()

@dp.callback_query(F.data == "donate_type_stars")
async def process_donate_stars(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("⭐️ **Пожертвование Telegram Stars**\n\nВведите количество звёзд цифрами (например: `50` или `100`):")
    await state.set_state(DonateForm.amount)
    await callback.answer()

@dp.message(DonateForm.amount)
async def process_donate_amount(message: types.Message, state: FSMContext):
    text_input = message.text.strip() if message.text else ""
    if not text_input.isdigit() or int(text_input) <= 0:
        await message.answer("⚠️ Ошибка! Введите корректное число звёзд цифрами:")
        return

    amount = int(text_input)
    prices = [types.LabeledPrice(label="Поддержка автора", amount=amount)]
    
    await bot.send_invoice(
        chat_id=message.from_user.id,
        title="Поддержка автора 💖",
        description=f"Добровольное пожертвование в размере {amount} Telegram Stars.",
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
    await message.answer("💖 **Огромное спасибо за поддержку! Вы помогаете проекту расти.**")

@dp.message()
async def search_music(message: types.Message):
    query = message.text.strip()
    if not query or query.startswith("/"):
        return

    wait_msg = await message.answer("🔍 Ищу трек по всей Сети...")

    try:
        def run_search():
            with yt_dlp.YoutubeDL(YDL_SEARCH_OPTIONS) as ydl:
                info = ydl.extract_info(query, download=False)
                return info.get('entries', [])

        entries = await asyncio.to_thread(run_search)

        if not entries:
            await wait_msg.edit_text("❌ Трек не найден. Попробуй написать точнее (например, имя исполнителя и трек раздельно).")
            return

        builder = InlineKeyboardBuilder()
        for entry in entries[:5]:
            if not entry:
                continue
            title = entry.get('title', 'Unknown Title')
            duration_sec = entry.get('duration')
            if duration_sec:
                mins = int(duration_sec) // 60
                secs = int(duration_sec) % 60
                duration = f"{mins}:{secs:02d}"
            else:
                duration = "??:??"
            
            video_id = entry.get('id')
            if not video_id:
                continue
            
            btn_text = f"🎵 {title[:35]} ({duration})"
            builder.button(text=btn_text, callback_data=f"dl_{video_id}")

        builder.button(text="💖 Поддержать автора", callback_data="start_donate")
        builder.adjust(1)

        await wait_msg.edit_text(
            f"🔎 Результаты по запросу: **{query}**\nВыберите трек для скачивания:",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )

    except Exception as e:
        log.error(f"Search error: {e}")
        await wait_msg.edit_text("⚠️ Ошибка при поиске музыки. Попробуйте другой запрос.")

@dp.callback_query(F.data.startswith("dl_"))
async def download_track(callback: types.CallbackQuery):
    video_id = callback.data.split("_", 1)[1]
    url = f"https://www.youtube.com/watch?v={video_id}"

    await callback.message.edit_text("📥 Скачиваю трек и конвертирую в MP3...")

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
            builder = get_ready_keyboard()
            
            await callback.message.answer_audio(
                audio=audio_file,
                caption=f"🎧 `{title}`\n\n📢 Канал автора: {CHANNEL_USERNAME}",
                reply_markup=builder.as_markup(),
                parse_mode="Markdown"
            )
            await callback.message.delete()
        else:
            await callback.message.edit_text("❌ Не удалось скачать файл.")

    except Exception as e:
        log.error(f"Download error: {e}")
        await callback.message.edit_text("⚠️ Ошибка при скачивании трека.")
    
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

async def main():
    await start_dummy_server()
    await set_main_menu(bot)
    log.info("🎵 Музыкальный бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
