"""Image resizing and short-lived public media URLs (served by web.py)."""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import secrets
import time

from PIL import Image, ImageOps

from .db import Database

log = logging.getLogger("smsgw.media")


def resize_to_jpeg(data: bytes, max_px: int, max_bytes: int) -> bytes:
    """Downscale to fit ``max_px`` on the long edge and re-encode JPEG under ``max_bytes``."""
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            if im.mode in ("RGBA", "LA") or "transparency" in im.info:
                im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1])
            else:
                bg.paste(im.convert("RGB"))
            im = bg
        im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        out = b""
        side = max(im.size)
        while True:
            for quality in (85, 75, 65, 55, 45):
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
                out = buf.getvalue()
                if len(out) <= max_bytes:
                    return out
            if side <= 320:
                return out
            side = int(side * 0.75)
            im.thumbnail((side, side), Image.Resampling.LANCZOS)


class MediaStore:
    def __init__(self, db: Database, directory: str, base_url: str) -> None:
        self.db = db
        self.dir = directory
        self.base_url = base_url.rstrip("/")
        os.makedirs(self.dir, exist_ok=True)

    async def publish(self, data: bytes, content_type: str, ttl_seconds: float, filename: str | None = None) -> str:
        token = secrets.token_urlsafe(24)
        ext = mimetypes.guess_extension(content_type) or ""
        if filename and "." in filename:
            ext = os.path.splitext(filename)[1][:10]
        path = os.path.join(self.dir, token)
        with open(path, "wb") as f:
            f.write(data)
        await self.db.add_media_token(token, path, content_type, time.time() + ttl_seconds)
        # Extension is cosmetic (some handsets show it); lookup is by token only.
        return f"{self.base_url}/m/{token}{ext}"

    async def cleanup(self) -> int:
        n = 0
        for row in await self.db.expired_media():
            try:
                os.remove(row["path"])
            except FileNotFoundError:
                pass
            await self.db.delete_media_token(row["token"])
            n += 1
        return n


# ---- ghost avatars ------------------------------------------------------------------------

# Mid-tone backgrounds that keep white initials readable (contrast >= 4.5:1).
AVATAR_COLORS = ["#1F6FB2", "#2E7D32", "#8E3B8E", "#B5481B", "#00796B", "#5D4FB3", "#A83246", "#5F6B23"]
AVATAR_PX = 256


def initials(name: str) -> str:
    words = [w for w in name.split() if w[:1].isalnum()]
    if not words:
        return "#"
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[-1][0]).upper()


def initials_avatar(name: str, seed: str) -> bytes:
    """A colored circle with the person's initials, as PNG. ``seed`` (the number) picks a stable color."""
    from PIL import ImageDraw, ImageFont

    color = AVATAR_COLORS[sum(seed.encode()) % len(AVATAR_COLORS)]
    im = Image.new("RGBA", (AVATAR_PX, AVATAR_PX), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, AVATAR_PX - 1, AVATAR_PX - 1), fill=color)
    text = initials(name)
    font = ImageFont.load_default(size=AVATAR_PX * 0.42 if len(text) > 1 else AVATAR_PX * 0.5)
    d.text((AVATAR_PX / 2, AVATAR_PX / 2), text, font=font, fill="white", anchor="mm")
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def square_avatar(data: bytes, px: int = 512) -> bytes:
    """Center-crop a photo to a square and re-encode JPEG (for #avatar)."""
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im = ImageOps.fit(im, (px, px), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85, optimize=True)
        return buf.getvalue()
