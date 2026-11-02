import os
import sqlite3
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import yt_dlp
import imageio_ffmpeg

# ================= НАСТРОЙКИ =================
TOKEN = "9123742962:AAGgcF7D89jxHG5YCKKl1GQOtxxIU7As0wY"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ================= БАЗА ДАННЫХ (SQLite) =================
conn = sqlite3.connect("music_bot.db")
cursor = conn.cursor()

# Таблица кэша треков
cursor.execute("""
CREATE TABLE IF NOT EXISTS tracks_cache (
    track_id TEXT PRIMARY KEY,
    file_id TEXT,
    title TEXT,
    uploader TEXT,
    url TEXT,
    thumbnail TEXT,
    platform TEXT
)
""")

# Таблица плейлистов пользователей
cursor.execute("""
CREATE TABLE IF NOT EXISTS playlists (
    user_id INTEGER,
    track_id TEXT,
    UNIQUE(user_id, track_id)
)
""")
conn.commit()


# ================= ПОИСК (YouTube Music + SoundCloud) =================
def search_music(query: str):
    """Ищет треки одновременно на YouTube Music и в SoundCloud"""
    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
        'noplaylist': True,
        'socket_timeout': 5,
    }
    
    combined_results = []
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # 1. Поиск на YouTube Music
        try:
            yt_info = ydl.extract_info(f"ytsearch3:{query} official audio", download=False)
            for item in yt_info.get('entries', []):
                if item:
                    item['source_platform'] = 'YouTube'
                    combined_results.append(item)
        except Exception as e:
            print(f"Ошибка поиска YouTube: {e}")

        # 2. Поиск в SoundCloud
        try:
            sc_info = ydl.extract_info(f"scsearch3:{query}", download=False)
            for item in sc_info.get('entries', []):
                if item:
                    item['source_platform'] = 'SoundCloud'
                    combined_results.append(item)
        except Exception as e:
            print(f"Ошибка поиска SoundCloud: {e}")
            
    return combined_results


def download_track(url: str, track_id: str):
    """Мгновенная многопоточная загрузка оригинального аудио без конвертации"""
    os.makedirs("downloads", exist_ok=True)
    outtmpl = f"downloads/{track_id}.%(ext)s"
    ydl_opts = {
        'format': 'bestaudio',
        'concurrent_fragment_downloads': 4,
        'socket_timeout': 10,
        'outtmpl': outtmpl,
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename, info.get('title', 'Track'), info.get('uploader', 'Музыкант')


# ================= ОБРАБОТЧИКИ КОМАНД И КНОПОК =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = ReplyKeyboardBuilder()
    kb.button(text="📂 Мой плейлист")
    await message.answer(
        "🎧 **Привет! Я твой музыкальный бот (YouTube Music + SoundCloud).**\n\n"
        "Напиши название песни, и я найду треки из обоих источников с обложками!\n"
        "Понравившуюся музыку можно сохранять в плейлист кнопкой ❤️.",
        reply_markup=kb.as_markup(resize_keyboard=True),
        parse_mode="Markdown"
    )


@dp.message(F.text == "📂 Мой плейлист")
async def show_playlist(message: types.Message):
    cursor.execute("""
        SELECT t.track_id, t.title, t.uploader, t.platform 
        FROM playlists p
        JOIN tracks_cache t ON p.track_id = t.track_id
        WHERE p.user_id = ?
    """, (message.from_user.id,))
    tracks = cursor.fetchall()

    if not tracks:
        await message.answer("Твой плейлист пока пуст. Найди песню и нажми ❤️ под ней!")
        return

    builder = InlineKeyboardBuilder()
    for track_id, title, uploader, platform in tracks:
        icon = "🎵" if platform == "YouTube" else "☁️"
        builder.button(text=f"{icon} {uploader} - {title}"[:60], callback_data=f"play:{track_id}")
    builder.adjust(1)

    await message.answer("🎧 **Твой сохраненный плейлист:**", reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.message(F.text)
async def handle_search(message: types.Message):
    status_msg = await message.answer(f"🔍 Ищу треки (YouTube + SoundCloud): *{message.text}*...", parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, search_music, message.text)

    if not results:
        await status_msg.edit_text("😔 Ничего не найдено. Попробуй уточнить запрос.")
        return

    builder = InlineKeyboardBuilder()
    for item in results:
        track_id = str(item.get('id'))
        title = item.get('title', 'Без названия')
        uploader = item.get('uploader', 'Исполнитель')
        platform = item.get('source_platform', 'YouTube')
        
        # Генерация ссылки в зависимости от платформы
        if platform == 'YouTube':
            url = item.get('url') or f"https://www.youtube.com/watch?v={track_id}"
        else:
            url = item.get('webpage_url') or item.get('url')

        thumbnails = item.get('thumbnails', [])
        thumbnail_url = thumbnails[-1]['url'] if thumbnails else None

        cursor.execute("""
            INSERT INTO tracks_cache (track_id, title, uploader, url, thumbnail, platform)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET url=excluded.url, thumbnail=excluded.thumbnail, platform=excluded.platform
        """, (track_id, title, uploader, url, thumbnail_url, platform))

        icon = "🎵" if platform == "YouTube" else "☁️"
        builder.button(text=f"{icon} [{platform}] {uploader} — {title}"[:60], callback_data=f"play:{track_id}")

    conn.commit()
    builder.adjust(1)
    await status_msg.edit_text("👇 Выбери трек для прослушивания:", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("play:"))
async def play_selected_track(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    cursor.execute("SELECT file_id, title, uploader, url, thumbnail FROM tracks_cache WHERE track_id = ?", (track_id,))
    row = cursor.fetchone()

    if not row:
        await callback.answer("Трек не найден в базе.", show_alert=True)
        return

    file_id, title, uploader, url, thumbnail = row

    kb = InlineKeyboardBuilder()
    kb.button(text="❤️ В плейлист", callback_data=f"fav:{track_id}")
    kb.button(text="🗑 Удалить", callback_data=f"unfav:{track_id}")
    kb.adjust(2)

    # Если файл уже есть в кэше Telegram — отправляем мгновенно
    if file_id:
        await callback.message.answer_audio(
            audio=file_id,
            title=title,
            performer=uploader,
            thumbnail=types.URLInputFile(thumbnail) if thumbnail else None,
            reply_markup=kb.as_markup()
        )
        await callback.answer()
        return

    await callback.answer("⏳ Скачиваю трек...")
    status = await callback.message.answer(f"⬇️ Загружаю: **{uploader} — {title}**...", parse_mode="Markdown")

    try:
        loop = asyncio.get_event_loop()
        filepath, real_title, real_uploader = await loop.run_in_executor(None, download_track, url, track_id)

        audio_file = types.FSInputFile(filepath)
        sent_msg = await callback.message.answer_audio(
            audio=audio_file,
            title=real_title,
            performer=real_uploader,
            thumbnail=types.URLInputFile(thumbnail) if thumbnail else None,
            reply_markup=kb.as_markup()
        )

        # Сохраняем file_id для быстрого доступа
        cursor.execute(
            "UPDATE tracks_cache SET file_id = ?, title = ?, uploader = ? WHERE track_id = ?",
            (sent_msg.audio.file_id, real_title, real_uploader, track_id)
        )
        conn.commit()

        if os.path.exists(filepath):
            os.remove(filepath)
        await status.delete()

    except Exception as e:
        await status.edit_text(f"❌ Ошибка загрузки: {e}")


@dp.callback_query(F.data.startswith("fav:"))
async def add_to_fav(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    cursor.execute("INSERT OR IGNORE INTO playlists (user_id, track_id) VALUES (?, ?)", (callback.from_user.id, track_id))
    conn.commit()
    await callback.answer("✅ Добавлено в плейлист!", show_alert=False)


@dp.callback_query(F.data.startswith("unfav:"))
async def remove_from_fav(callback: types.CallbackQuery):
    track_id = callback.data.split(":")[1]
    cursor.execute("DELETE FROM playlists WHERE user_id = ? AND track_id = ?", (callback.from_user.id, track_id))
    conn.commit()
    await callback.answer("🗑 Удалено из плейлиста.", show_alert=False)


# ================= ЗАПУСК =================
import os
from aiohttp import web
import asyncio

# Простой веб-сервер, который сразу отвечает Render, что порт открыт
async def handle(request):
    return web.Response(text="Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    # Render передает свой порт через переменные окружения, по умолчанию берем 10000
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Веб-сервер запущен на порту {port}")

async def main():
   # 1. Сначала запускаем веб-сервер для Render, чтобы он сразу увидел порт
    await start_web_server()
    print("🚀 Музыкальный бот запущен и слушает Telegram!")
    
    # 2. Затем запускаем самого бота
    await dp.start_polling(bot)
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Ошибка при запуске: {e}")
