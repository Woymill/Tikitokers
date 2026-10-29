import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo

# ---------- Настройки (переменные окружения) ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = (
    os.environ.get("BOT_TOKEN")
    or os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TOKEN")
)
if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN")

# Владельцы: расширенные лимиты. Можно задать OWNER_IDS="691338945,123"
OWNER_IDS = {
    int(x) for x in os.environ.get("OWNER_IDS", "691338945").split(",") if x.strip()
}

# Видео обрабатывается в оперативной памяти (/dev/shm), на диск не пишется.
# Поэтому лимиты зависят от RAM вашего тарифа — настройте под себя.
USER_MAX_MB = int(os.environ.get("USER_MAX_MB", "100"))
USER_MAX_SEC = int(os.environ.get("USER_MAX_SEC", "180"))
OWNER_MAX_MB = int(os.environ.get("OWNER_MAX_MB", "500"))
OWNER_MAX_SEC = int(os.environ.get("OWNER_MAX_SEC", "3600"))

# Временная папка в RAM; если её нет — обычная временная папка
TMP_BASE = "/dev/shm" if os.access("/dev/shm", os.W_OK) else None

# Одновременно обрабатываем не больше N видео
semaphore = asyncio.Semaphore(int(os.environ.get("MAX_JOBS", "2")))

# Вертикаль 1080x1920 с размытым фоном, шумоподавление, резкость, цвет
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

Path("data").mkdir(exist_ok=True)
client = TelegramClient("data/bot", API_ID, API_HASH)


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


async def probe_duration(path: Path) -> int:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return int(float(json.loads(out)["format"]["duration"]))
    except Exception:
        return 0


def is_video(event) -> bool:
    if event.video:
        return True
    doc = event.document
    return bool(doc and (doc.mime_type or "").startswith("video/"))


@client.on(events.NewMessage(pattern="/start", incoming=True))
async def start(event):
    await event.respond(
        "Привет! Пришли видео — я подготовлю его для TikTok:\n"
        "• формат 1080×1920 (9:16)\n"
        "• шумоподавление и резкость\n"
        "• цвет и контраст\n"
        "• выровненная громкость\n\n"
        f"Лимит: до {USER_MAX_MB} МБ и {USER_MAX_SEC} сек."
    )


@client.on(events.NewMessage(incoming=True, func=is_video))
async def handle_video(event):
    is_owner = event.sender_id in OWNER_IDS
    max_mb = OWNER_MAX_MB if is_owner else USER_MAX_MB
    max_sec = OWNER_MAX_SEC if is_owner else USER_MAX_SEC

    f = event.file
    if f.size and f.size > max_mb * 1024 * 1024:
        await event.reply(f"Файл больше {max_mb} МБ.")
        return
    if f.duration and f.duration > max_sec:
        await event.reply(f"Видео длиннее {max_sec} сек.")
        return

    status = await event.reply("⏳ Скачиваю видео...")

    async with semaphore:
        with tempfile.TemporaryDirectory(dir=TMP_BASE) as tmp:
            src = Path(tmp) / "in.mp4"
            dst = Path(tmp) / "out.mp4"
            try:
                await event.download_media(file=str(src))
                await status.edit("⚙️ Улучшаю качество, это может занять время...")
                try:
                    await enhance(src, dst)
                finally:
                    src.unlink(missing_ok=True)  # оригинал удаляем сразу

                await status.edit("📤 Отправляю результат...")
                duration = await probe_duration(dst)
                await client.send_file(
                    event.chat_id,
                    str(dst),
                    caption="Готово ✅",
                    reply_to=event.id,
                    attributes=[
                        DocumentAttributeVideo(
                            duration=duration,
                            w=1080,
                            h=1920,
                            supports_streaming=True,
                        )
                    ],
                    supports_streaming=True,
                )
                dst.unlink(missing_ok=True)  # результат удаляем сразу после отправки
                await status.delete()
            except Exception:
                logging.exception("Ошибка обработки")
                await status.edit("Не получилось обработать видео 😕")


async def main():
    logging.basicConfig(level=logging.INFO)
    await client.start(bot_token=BOT_TOKEN)
    logging.info("Бот запущен")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
