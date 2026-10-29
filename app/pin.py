"""Pinned "Voice Chat" message in each group, styled like Telegram's call bar.

One message per group, never deleted: it is edited a few seconds after anyone joins or leaves.
The photo shows up to 3 participants' profile pictures, so the small thumbnail in the pinned
bar looks like Telegram's own voice chat bar.
"""
import asyncio
import functools
import io
import json
import logging
import os
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import config, db, tg

log = logging.getLogger("pin")
LABEL = os.getenv("VC_LABEL", "Voice Chat").strip() or "Voice Chat"
W, H = 1200, 630
_pending: dict[int, asyncio.Task] = {}
_last: dict[int, tuple] = {}
_edit_lock: dict[int, asyncio.Lock] = {}

FONT_DIRS = ["/usr/share/fonts/opentype/inter", "/usr/share/fonts/truetype/inter", "/usr/share/fonts/truetype/dejavu"]


@functools.lru_cache(maxsize=16)
def _font(size: int, bold: bool = True):
    names = (["Inter-Bold.otf", "Inter-SemiBold.otf", "InterDisplay-Bold.otf"] if bold else ["Inter-Medium.otf", "Inter-Regular.otf"]) + \
            (["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"])
    for d in FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def _initial_avatar(name: str, uid: int, size: int) -> Image.Image:
    grads = [((255, 94, 98), (255, 153, 102)), ((95, 114, 189), (155, 35, 234)), ((17, 153, 142), (56, 239, 125)),
             ((252, 92, 125), (106, 130, 251)), ((247, 151, 30), (255, 210, 0)), ((0, 198, 255), (0, 114, 255))]
    a, b = grads[abs(uid) % len(grads)]
    im = Image.new("RGB", (size, size), a)
    d = ImageDraw.Draw(im)
    for y in range(size):
        k = y / size
        d.line([(0, y), (size, y)], fill=tuple(int(a[i] * (1 - k) + b[i] * k) for i in range(3)))
    parts = (name or "?").split()
    ini = ((parts[0][:1] if parts else "?") + (parts[1][:1] if len(parts) > 1 else "")).upper()
    f = _font(int(size * 0.4))
    d.text((size / 2, size / 2), ini, font=f, fill="white", anchor="mm")
    return im


def _circle(im: Image.Image, size: int, ring: int, ring_color) -> Image.Image:
    im = im.convert("RGB")
    s = min(im.size)
    im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2)).resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size + ring * 2, size + ring * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    d.ellipse([0, 0, size + ring * 2 - 1, size + ring * 2 - 1], fill=ring_color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out.paste(im, (ring, ring), mask)
    return out


def _mic_icon(size: int) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([0, 0, size - 1, size - 1], fill=(43, 143, 232))
    w = size * 0.22
    cx = size / 2
    d.rounded_rectangle([cx - w / 2, size * 0.22, cx + w / 2, size * 0.58], radius=w / 2, fill="white")
    lw = max(4, size // 22)
    d.arc([cx - size * 0.2, size * 0.32, cx + size * 0.2, size * 0.68], 0, 180, fill="white", width=lw)
    d.line([cx, size * 0.68, cx, size * 0.78], fill="white", width=lw)
    return im


@functools.lru_cache(maxsize=2)
def _background(live: bool) -> Image.Image:
    """Gradient + blurred glow. Same for every render, so it is built once (the blur is the slow part)."""
    bg = Image.new("RGB", (W, H), (18, 24, 31))
    d = ImageDraw.Draw(bg)
    for y in range(H):  # soft blue glow from the top
        k = y / H
        d.line([(0, y), (W, y)], fill=(int(30 - 12 * k), int(52 - 28 * k), int(84 - 53 * k)))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W / 2 - 330, 40, W / 2 + 330, 520], fill=(77, 162, 241, 70) if live else (77, 162, 241, 35))
    return Image.alpha_composite(bg.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(90)))


async def render(chat_id: int, people: list[dict], count: int, sub: str) -> bytes:
    """people: up to 3 dicts with uid, name. Returns JPEG bytes."""
    paths = []
    for p in people[:3]:
        try:
            path = await tg.avatar_path(p["uid"], p.get("photo_url") or "") if (p["uid"] > 0 or p.get("group")) else None
        except Exception:
            path = None
        if config.DEV_MODE and not path:
            dev = os.path.join(tg.AV_DIR, f"{p['uid']}.jpg")
            path = dev if os.path.exists(dev) else None
        paths.append(path)
    # Pillow work is CPU-bound: run it off the event loop so voice/chat never stall while the image is drawn.
    return await asyncio.to_thread(_render_sync, people[:3], paths, count, sub)


def _avatar_src(path: str | None, p: dict, size: int) -> Image.Image:
    if path:
        try:
            with Image.open(path) as im:
                im.load()
                return im.copy()
        except Exception:
            pass
    return _initial_avatar(p.get("name", ""), p["uid"], size)


def _render_sync(people: list[dict], paths: list, count: int, sub: str) -> bytes:
    bg = _background(bool(count)).copy()
    size, ring = 210, 12
    cy = 250
    if people:
        tiles = []
        for p, path in zip(people, paths):
            tiles.append(_circle(_avatar_src(path, p, size), size, ring, (18, 24, 31, 255)))
        step = int(size * 0.68)
        total = size + ring * 2 + step * (len(tiles) - 1)
        x0 = (W - total) // 2
        for i, t in enumerate(reversed(tiles)):  # first person on top, like Telegram
            idx = len(tiles) - 1 - i
            bg.alpha_composite(t, (x0 + idx * step, cy - (size + ring * 2) // 2))
    else:
        icon = _mic_icon(200)
        bg.alpha_composite(icon, ((W - 200) // 2, cy - 100))

    d = ImageDraw.Draw(bg)
    d.text((W / 2, 455), LABEL, font=_font(64), fill="white", anchor="mm")
    sub_col = (77, 162, 241) if count else (131, 148, 166)
    d.text((W / 2, 530), sub, font=_font(40, bold=False), fill=sub_col, anchor="mm")
    if count:  # live dot
        tw = d.textlength(sub, font=_font(40, bold=False))
        x = W / 2 - tw / 2 - 30
        d.ellipse([x - 10, 520, x + 10, 540], fill=(79, 209, 122))
    out = io.BytesIO()
    bg.convert("RGB").save(out, "JPEG", quality=88)
    return out.getvalue()


def caption_for(count: int, sub: str) -> str:
    return f"<b>{LABEL}</b> · {sub}"


async def _state(chat_id: int) -> tuple[list[dict], int, str]:
    from .room import ROOMS
    r = ROOMS.get(chat_id)
    parts = list(r.parts.values()) if r else []
    parts.sort(key=lambda p: (not (p.mic and r.can_speak(p)), p.joined))
    people = []
    for p in parts[:3]:
        if p.as_group:
            people.append({"uid": chat_id, "name": r.group_title, "group": True})
        else:
            u = await db.q1("SELECT photo FROM users WHERE uid=?", (p.uid,))
            people.append({"uid": p.uid, "name": p.name, "photo_url": (u or {}).get("photo", "")})
    n = len(parts)
    if n:
        sub = f"{n} participant" + ("" if n == 1 else "s")
    else:
        sch = await db.q1("SELECT at FROM schedules WHERE chat_id=? AND sent=0 ORDER BY at LIMIT 1", (chat_id,))
        if sch and sch["at"] - time.time() < 7 * 86400:
            mins = max(1, int((sch["at"] - time.time()) / 60))
            sub = f"Starts in {mins // 60}h {mins % 60}m" if mins >= 60 else f"Starts in {mins}m"
        else:
            sub = "No participants"
    return people, n, sub


async def _keyboard(chat_id: int) -> dict:
    url = await tg.group_link(chat_id)
    return {"inline_keyboard": [[{"text": "JOIN", "url": url}]]}


async def post(chat_id: int, thread_id: int | None = None, repin_only: bool = False):
    """/vc: re-pin the existing message if we have one, otherwise create it."""
    g = await db.get_group(chat_id)
    mid = g.get("pin_msg")
    if thread_id is None:
        thread_id = g.get("pin_thread")
    if mid:
        try:
            await tg.call("pinChatMessage", chat_id=chat_id, message_id=mid, disable_notification=True)
            await refresh(chat_id, force=True)
            return mid
        except Exception as e:
            log.info("re-pin failed (%s), posting new", e)
    people, n, sub = await _state(chat_id)
    img = await render(chat_id, people, n, sub)
    data = {"chat_id": str(chat_id), "caption": caption_for(n, sub), "parse_mode": "HTML",
            "disable_notification": "true", "reply_markup": json.dumps(await _keyboard(chat_id))}
    if thread_id:
        data["message_thread_id"] = str(thread_id)
    msg = await tg.call_multipart("sendPhoto", data, {"photo": ("voicechat.jpg", img, "image/jpeg")})
    mid = msg["message_id"]
    await db.x("UPDATE groups SET pin_msg=?, pin_thread=? WHERE chat_id=?", (mid, thread_id, chat_id))
    try:
        await tg.call("pinChatMessage", chat_id=chat_id, message_id=mid, disable_notification=True)
    except Exception as e:
        log.info("pin failed (bot needs 'Pin messages' admin right): %s", e)
    _last[chat_id] = (n, tuple(p["uid"] for p in people), sub)
    return mid


async def refresh(chat_id: int, force: bool = False):
    g = await db.get_group(chat_id)
    mid = g.get("pin_msg")
    if not mid or not config.BOT_TOKEN:
        return
    people, n, sub = await _state(chat_id)
    key = (n, tuple(p["uid"] for p in people), sub)
    if not force and _last.get(chat_id) == key:
        return
    lock = _edit_lock.setdefault(chat_id, asyncio.Lock())
    async with lock:
        img = await render(chat_id, people, n, sub)
        media = {"type": "photo", "media": "attach://photo", "caption": caption_for(n, sub), "parse_mode": "HTML"}
        try:
            await tg.call_multipart("editMessageMedia", {"chat_id": str(chat_id), "message_id": str(mid), "media": json.dumps(media),
                                                         "reply_markup": json.dumps(await _keyboard(chat_id))},
                                    {"photo": ("voicechat.jpg", img, "image/jpeg")})
            _last[chat_id] = key
        except Exception as e:
            msg = str(e)
            if "not modified" in msg:
                _last[chat_id] = key
            elif "not found" in msg or "MESSAGE_ID_INVALID" in msg:
                await db.x("UPDATE groups SET pin_msg=NULL WHERE chat_id=?", (chat_id,))
            else:
                log.info("pin edit failed: %s", msg)


def schedule(chat_id: int, delay: float = 3.0):
    """Debounced refresh: many joins/leaves within a few seconds become one edit."""
    t = _pending.get(chat_id)
    if t and not t.done():
        return

    async def run():
        await asyncio.sleep(delay)
        _pending.pop(chat_id, None)
        try:
            await refresh(chat_id)
        except Exception:
            log.exception("pin refresh")

    _pending[chat_id] = asyncio.create_task(run())


async def startup_reset():
    """After a restart nobody is connected: show every pinned bar as empty."""
    await asyncio.sleep(5)
    for g in await db.q("SELECT chat_id FROM groups WHERE pin_msg IS NOT NULL"):
        try:
            await refresh(g["chat_id"], force=True)
        except Exception:
            pass
        await asyncio.sleep(1)


async def countdown_loop():
    """Keep 'Starts in …' fresh for scheduled chats."""
    while True:
        await asyncio.sleep(60)
        try:
            for g in await db.q("SELECT DISTINCT s.chat_id FROM schedules s JOIN groups g ON g.chat_id=s.chat_id "
                                "WHERE s.sent=0 AND g.pin_msg IS NOT NULL"):
                schedule(g["chat_id"], 0)
        except Exception:
            log.exception("countdown loop")
