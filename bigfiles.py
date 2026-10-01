"""Downloads files above the Bot API's 20 MB limit over MTProto, logged in as the bot.

The Bot API refuses downloads above 20 MB, but the same bot account signed in through
MTProto (with an api_id/api_hash from my.telegram.org) can fetch files up to 2 GB.
"""
import asyncio
import os
import subprocess
import threading

API_ID = int(os.environ.get("TG_API_ID") or 0)
API_HASH = os.environ.get("TG_API_HASH", "")
MAX_BYTES = 1024 * 1024 * 1024
COMPRESS_ABOVE = 20 * 1024 * 1024


def available():
    return bool(API_ID and API_HASH)


class Downloader:
    def __init__(self, bot_token, load_session, save_session):
        self.bot_token = bot_token
        self.load_session = load_session
        self.save_session = save_session
        self.loop = None
        self.client = None
        self.lock = threading.Lock()

    def _ensure_loop(self):
        with self.lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                threading.Thread(target=self.loop.run_forever, daemon=True).start()

    async def _get_client(self):
        if self.client is None:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            saved = self.load_session() or ""
            client = TelegramClient(StringSession(saved), API_ID, API_HASH)
            # Reusing the stored session avoids a new bot login, which Telegram rate-limits
            await client.start(bot_token=self.bot_token)
            fresh = client.session.save()
            if fresh != saved:
                self.save_session(fresh)
            self.client = client
        return self.client

    async def _download(self, chat_id, message_id, path):
        from telethon.tl.types import PeerChannel, PeerChat
        client = await self._get_client()
        cid = str(chat_id)
        peer = PeerChannel(int(cid[4:])) if cid.startswith("-100") else PeerChat(-int(cid))
        msg = await client.get_messages(peer, ids=int(message_id))
        if msg is None or msg.media is None:
            raise LookupError(f"message {chat_id}/{message_id} has no media")
        return await client.download_media(msg, file=path)

    def download(self, chat_id, message_id, path, timeout=900):
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._download(chat_id, message_id, path), self.loop)
        return fut.result(timeout)


def compress(src, mime):
    """Shrinks a large take; returns (path, mime, ext), or the original when that is smaller."""
    if os.path.getsize(src) <= COMPRESS_ABOVE:
        return None
    if mime.startswith("video/"):
        dst, out_mime, ext = src + ".small.mp4", "video/mp4", "mp4"
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", src,
               "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", dst]
    else:
        dst, out_mime, ext = src + ".small.ogg", "audio/ogg", "ogg"
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-c:a", "libopus", "-b:a", "160k", dst]
    subprocess.run(cmd, check=True, timeout=1800, capture_output=True)
    if os.path.getsize(dst) >= os.path.getsize(src):
        os.remove(dst)
        return None
    return dst, out_mime, ext
