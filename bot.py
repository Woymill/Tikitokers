import asyncio
import logging
import os
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message

BOT_TOKEN = (
    os.environ.get("BOT_TOKEN")
    or os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TOKEN")
)
if not BOT_TOKEN:
    raise SystemExit("Не задан токен: добавьте переменную BOT_TOKEN")

# Лимит скачивания у обычного Bot API — 20 МБ.
# Если поднимете локальный Bot API сервер, можно увеличить.
MAX_INPUT_MB = 20
MAX_DURATION_SEC = 180

# Одновременно обрабатываем не больше N видео (ffmpeg тяжёлый)
semaphore = asyncio.Semaphore(2)

# Фильтры: вертикаль 1080x1920 с размытым фоном, шумоподавление,
# лёгкая резкость, контраст и насыщенность
VIDEO_FILTER = (
    "split[a][b];"
    "[a]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
    "crop=1080:1920,boxblur=25:5[bg];"
    "[b]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
    "hqdn3d=2:1.5:3:3,"
    "unsharp=5:5:0.8:5:5:0.0,"
    "eq=contrast=1.06:saturation=1.15:brightness=0.01,"
    "format=yuv420p"
)

dp = Dispatcher()


async def enhance(src: Path, dst: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", VIDEO_FILTER,
        "-r", "30",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-profile:v", "high", "-maxrate", "12M", "-bufsize", "24M",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-movflags", "+faststart",
        str(dst),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="ignore")[-500:])


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Пришли видео — я подготовлю его для TikTok:\n"
        "• формат 1080×1920 (9:16)\n"
        "• шумоподавление и резкость\n"
        "• цвет и контраст\n"
        "• выровненная громкость\n\n"
        f"Лимит: до {MAX_INPUT_MB} МБ и {MAX_DURATION_SEC} сек."
    )


@dp.message(F.video | F.document.mime_type.startswith("video/"))
async def handle_video(message: Message, bot: Bot):
    media = message.video or message.document

    if media.file_size and media.file_size > MAX_INPUT_MB * 1024 * 1024:
        await message.answer(f"Файл больше {MAX_INPUT_MB} МБ, пришли поменьше.")
        return
    duration = getattr(media, "duration", None)
    if duration and duration > MAX_DURATION_SEC:
        await message.answer(f"Видео длиннее {MAX_DURATION_SEC} сек.")
        return

    status = await message.answer("⏳ Обрабатываю, это может занять минуту...")

    async with semaphore:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp4"
            dst = Path(tmp) / "out.mp4"
            try:
                await bot.download(media, destination=src)
                await enhance(src, dst)
                await message.answer_video(
                    FSInputFile(dst),
                    width=1080,
                    height=1920,
                    supports_streaming=True,
                    caption="Готово ✅",
                )
                await status.delete()
            except Exception:
                logging.exception("Ошибка обработки")
                await status.edit_text("Не получилось обработать видео 😕")


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
