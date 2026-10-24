import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp

# ================= Настройки =================
BOT_TOKEN = "ТВОЙ_ТОКЕН_МУЗЫКАЛЬНОГО_БОТА"
# =============================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Настройки для yt-dlp (поиск в YouTube Music / YouTube)
YDL_SEARCH_OPTIONS = {
    'format': 'bestaudio/best',
    'default_search': 'ytsearch5',  # Ищет топ-5 результатов по запросу
    'noplaylist': True,
    'quiet': True,
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
}

os.makedirs('downloads', exist_ok=True)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🎵 **Привет! Я музыкальный бот.**\n\n"
        "Просто отправь мне название трека или имя исполнителя (например: *Scally Milano* или *Kizaru*), "
        "и я найду для тебя музыку!"
    )

@dp.message()
async def search_music(message: types.Message):
    query = message.text.strip()
    if not query or query.startswith("/"):
        return

    wait_msg = await message.answer("🔍 Ищу треки в Сети...")

    try:
        # Ищем треки через yt-dlp в асинхронном режиме
        def run_search():
            with yt_dlp.YoutubeDL(YDL_SEARCH_OPTIONS) as ydl:
                info = ydl.extract_info(query, download=False)
                return info.get('entries', [])

        entries = await asyncio.to_thread(run_search)

        if not entries:
            await wait_msg.edit_text("❌ Ничего не найдено по твоему запросу.")
            return

        builder = InlineKeyboardBuilder()
        for i, entry in enumerate(entries[:5]):
            title = entry.get('title', 'Unknown Title')
            duration = entry.get('duration_string', '??:??')
            video_id = entry.get('id')
            
            # Ограничиваем длину названия для кнопки
            btn_text = f"🎵 {title[:35]} ({duration})"
            builder.button(text=btn_text, callback_data=f"dl_{video_id}")

        builder.adjust(1)
        await wait_msg.edit_text(
            f"🔎 Результаты по запросу: **{query}**\nВыбери трек для скачивания:",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Search error: {e}")
        await wait_msg.edit_text("⚠️ Ошибка при поиске музыки. Попробуй другой запрос.")

@dp.callback_query(F.data.startswith("dl_"))
async def download_track(callback: types.CallbackQuery):
    video_id = callback.data.split("_")[1]
    url = f"https://www.youtube.com/watch?v={video_id}"

    await callback.message.edit_text("📥 Скачиваю трек и конвертирую в MP3...")

    file_path = None
    try:
        def run_download():
            with yt_dlp.YoutubeDL(YDL_DOWNLOAD_OPTIONS) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                # Меняем расширение на mp3 после ffmpeg конвертации
                base, _ = os.path.splitext(filename)
                return f"{base}.mp3", info.get('title', 'music')

        file_path, title = await asyncio.to_thread(run_download)

        if os.path.exists(file_path):
            audio_file = types.FSInputFile(file_path)
            await callback.message.answer_audio(
                audio=audio_file,
                caption=f"🎧 `{title}`",
                parse_mode="Markdown"
            )
            await callback.message.delete()
        else:
            await callback.message.edit_text("❌ Не удалось скачать файл.")

    except Exception as e:
        logging.error(f"Download error: {e}")
        await callback.message.edit_text("⚠️ Ошибка при скачивании трека.")
    
    finally:
        # Чистим за собой место на сервере, чтобы память не забивалась
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

async def main():
    print("🎵 Музыкальный бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
