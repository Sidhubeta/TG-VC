"""YouTube via yt-dlp: search, playlists, stream resolving, audio cache, range proxy, lyrics."""
import asyncio
import base64
import logging
import os
import re
import time
from collections import OrderedDict
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse

from . import config
from .http import httpx

log = logging.getLogger("music")

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

CACHE_DIR = os.path.join(config.DATA_DIR, "audio")
os.makedirs(CACHE_DIR, exist_ok=True)
COOKIE_FILE = os.path.join(config.DATA_DIR, "cookies.txt")
if config.YT_COOKIES_B64:
    with open(COOKIE_FILE, "wb") as _fh:
        _fh.write(base64.b64decode(config.YT_COOKIES_B64))
elif config.YT_COOKIES:
    with open(COOKIE_FILE, "w") as _fh:
        _fh.write(config.YT_COOKIES.replace("\\n", "\n"))

_info_cache: dict[str, tuple[float, dict]] = {}
STATS: dict = {"yt_ok_at": 0.0, "yt_err_at": 0.0, "yt_err": ""}  # for the admins' Health page
_downloads: dict[str, asyncio.Task] = {}
_resolve_locks: dict[str, asyncio.Lock] = {}
_http: httpx.AsyncClient | None = None
CHUNK = 8 * 1024 * 1024  # googlevideo throttles big open ranges; serve in 8MB pieces
VID_RE = re.compile(r"[\w-]{11}")


def valid_id(vid: str) -> bool:
    return bool(vid) and bool(VID_RE.fullmatch(vid))


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
    return _http


class _QuietLog:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): log.info("yt-dlp: %s", str(msg)[:300])


def _opts(**extra) -> dict:
    o = {"logger": _QuietLog(), "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
         "socket_timeout": 15, "retries": 3, "extractor_retries": 2}
    if os.path.exists(COOKIE_FILE):
        o["cookiefile"] = COOKIE_FILE
    if config.POT_PROVIDER_URL:
        o["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [config.POT_PROVIDER_URL]}}
    o.update(extra)
    return o


def _thumb(vid: str) -> str:
    return f"/api/thumb/{vid}"


def _track(e: dict) -> dict | None:
    vid = e.get("id")
    if not vid or len(vid) != 11:
        return None
    if e.get("live_status") in ("is_live", "is_upcoming"):
        return None
    artist = e.get("artist") or e.get("channel") or e.get("uploader") or ""
    artist = re.sub(r"\s*-\s*Topic$", "", artist)
    return {"id": vid, "title": e.get("track") or e.get("title") or "Unknown", "artist": artist,
            "duration": float(e.get("duration") or 0), "thumb": _thumb(vid)}


def parse_youtube(text: str) -> tuple[str | None, str | None]:
    """Returns (video_id, playlist_id) from any YouTube / YT Music URL."""
    text = text.strip()
    if re.fullmatch(r"[\w-]{11}", text):
        return text, None
    try:
        u = urlparse(text if "://" in text else "https://" + text)
    except Exception:
        return None, None
    host = (u.hostname or "").lower()
    if not any(h in host for h in ("youtube.com", "youtu.be")):
        return None, None
    qs = parse_qs(u.query)
    vid = None
    if "youtu.be" in host:
        vid = u.path.strip("/")[:11]
    elif "v" in qs:
        vid = qs["v"][0][:11]
    elif m := re.match(r"/(shorts|live|embed)/([\w-]{11})", u.path):
        vid = m.group(2)
    if vid and not valid_id(vid):
        vid = None
    pl = qs.get("list", [None])[0]
    if pl and not re.fullmatch(r"[\w-]{10,64}", pl):
        pl = None
    if pl and pl.startswith("RD"):  # auto mixes: treat as single video
        pl = None
    return vid, pl


# ---- Spotify links: read the song names, find each song on YouTube ------------------------------------------
SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)(playlist|album|track)[/:]([A-Za-z0-9]{22})")
_sp_token: tuple[float, str] = (0.0, "")


def parse_spotify(text: str) -> tuple[str, str] | None:
    m = SPOTIFY_RE.search(text or "")
    return (m.group(1), m.group(2)) if m else None


async def _spotify_api(kind: str, sid: str, limit: int) -> tuple[str, list[dict]]:
    global _sp_token
    if time.time() > _sp_token[0]:
        r = await http().post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                              auth=(config.SPOTIFY_CLIENT_ID, config.SPOTIFY_CLIENT_SECRET))
        r.raise_for_status()
        j = r.json()
        _sp_token = (time.time() + j.get("expires_in", 3600) - 60, j["access_token"])
    h = {"Authorization": f"Bearer {_sp_token[1]}"}
    api = "https://api.spotify.com/v1"
    one = lambda t: {"title": t.get("name") or "", "artist": ", ".join(a["name"] for a in t.get("artists") or []),  # noqa: E731
                     "duration": round((t.get("duration_ms") or 0) / 1000)}
    if kind == "track":
        t = (await http().get(f"{api}/tracks/{sid}", headers=h)).raise_for_status().json()
        return t.get("name") or "Song", [one(t)]
    meta = (await http().get(f"{api}/{kind}s/{sid}", headers=h, params={"fields": "name"} if kind == "playlist" else None)).raise_for_status().json()
    out, url = [], f"{api}/{kind}s/{sid}/tracks?limit={50 if kind == 'album' else 100}"
    while url and len(out) < limit:
        j = (await http().get(url, headers=h)).raise_for_status().json()
        for it in j.get("items") or []:
            t = it.get("track") if kind == "playlist" else it
            if t and t.get("name") and t.get("type", "track") == "track":
                out.append(one(t))
        url = j.get("next")
    return meta.get("name") or "Spotify", out[:limit]


def _find(obj, key):
    """First value stored under `key` anywhere in a JSON tree."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)) and (r := _find(v, key)) is not None:
                return r
    return None


def parse_spotify_embed(html: str) -> tuple[str, list[dict]]:
    import json as _json
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("Spotify page not understood")
    data = _json.loads(m.group(1))
    entity = _find(data, "entity") or {}
    title = entity.get("name") or entity.get("title") or "Spotify"
    tl = entity.get("trackList") or _find(data, "trackList")
    if tl:
        return title, [{"title": t.get("title") or "", "artist": t.get("subtitle") or "",
                        "duration": round((t.get("duration") or 0) / 1000)} for t in tl if t.get("title")]
    # a single song
    artists = ", ".join(a.get("name", "") for a in entity.get("artists") or [])
    return title, [{"title": title, "artist": artists, "duration": round((entity.get("duration") or 0) / 1000)}]


async def spotify(kind: str, sid: str, limit: int = 200) -> tuple[str, list[dict]]:
    """(name, [{title, artist, duration}]) for a Spotify playlist / album / song."""
    if config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET:
        try:
            return await _spotify_api(kind, sid, limit)
        except Exception as e:
            log.info("spotify api failed (%s), using the embed page", e)
    r = await http().get(f"https://open.spotify.com/embed/{kind}/{sid}",
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"})
    if r.status_code != 200:
        raise ValueError("Spotify didn't answer — is the link right and public?")
    name, tracks = parse_spotify_embed(r.text)
    return name, tracks[:limit]


async def match(song: dict) -> dict | None:
    """The YouTube upload that best fits a song name (+ artist), preferring the same length."""
    q = f"{song['title']} {song['artist']}".strip()
    try:
        res = await search(q, limit=4)
    except Exception as e:
        log.info("match failed %r: %s", q, e)
        return None
    if not res:
        return None
    d = song.get("duration") or 0
    if d:
        close = [t for t in res if t.get("duration") and abs(t["duration"] - d) <= max(8, d * 0.06)]
        if close:
            return close[0]
    return res[0]


def _extract(url: str, **extra) -> dict:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed")
    with yt_dlp.YoutubeDL(_opts(**extra)) as y:
        return y.extract_info(url, download=False)


async def search(query: str, limit: int = 15) -> list[dict]:
    info = await asyncio.to_thread(_extract, f"ytsearch{limit}:{query}", extract_flat="in_playlist")
    return [t for e in info.get("entries") or [] if (t := _track(e))]


async def playlist(pid: str, limit: int = 200) -> tuple[str, list[dict]]:
    info = await asyncio.to_thread(_extract, f"https://www.youtube.com/playlist?list={pid}",
                                   extract_flat="in_playlist", noplaylist=False, playlistend=limit)
    return info.get("title") or "Playlist", [t for e in info.get("entries") or [] if (t := _track(e))]


async def related(vid: str, limit: int = 25) -> list[dict]:
    info = await asyncio.to_thread(_extract, f"https://www.youtube.com/watch?v={vid}&list=RD{vid}",
                                   extract_flat="in_playlist", noplaylist=False, playlistend=limit)
    return [t for e in info.get("entries") or [] if (t := _track(e)) and t["id"] != vid]


def is_ready(vid: str) -> bool:
    """The stream address is already known, so the song can start right away."""
    hit = _info_cache.get(vid)
    return bool(hit) and time.time() - hit[0] < 3 * 3600


async def resolve(vid: str, force: bool = False) -> dict:
    hit = _info_cache.get(vid)
    if hit and not force and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    if len(_resolve_locks) > 1000:  # forget idle locks so the dict doesn't grow forever
        for k in [k for k, lk in _resolve_locks.items() if not lk.locked()]:
            _resolve_locks.pop(k, None)
    lock = _resolve_locks.setdefault(vid, asyncio.Lock())
    async with lock:
        hit = _info_cache.get(vid)
        if hit and not force and time.time() - hit[0] < 3 * 3600:
            return hit[1]
        try:
            info = await asyncio.to_thread(_extract, f"https://www.youtube.com/watch?v={vid}")
        except Exception as e:
            STATS["yt_err"], STATS["yt_err_at"] = str(e)[:300], time.time()
            raise
        STATS["yt_ok_at"] = time.time()
        fmts = []
        for f in info.get("formats") or []:
            if not f.get("url") or f.get("protocol") not in ("https", "http"):
                continue
            fmts.append({"id": f.get("format_id"), "url": f["url"], "ext": f.get("ext"),
                         "acodec": f.get("acodec") or "none", "vcodec": f.get("vcodec") or "none",
                         "height": f.get("height") or 0, "abr": f.get("abr") or 0, "tbr": f.get("tbr") or 0,
                         "headers": f.get("http_headers") or {}})
        out = {"track": _track(info), "formats": fmts}
        _info_cache[vid] = (time.time(), out)
        if len(_info_cache) > 500:
            for k, _ in sorted(_info_cache.items(), key=lambda kv: kv[1][0])[:100]:
                _info_cache.pop(k, None)
        return out


def pick_audio(info: dict, codec: str = "aac", quality: str = "medium") -> dict | None:
    auds = [f for f in info["formats"] if f["vcodec"] == "none" and f["acodec"] != "none"]
    if codec == "aac":
        pref = [f for f in auds if f["ext"] == "m4a" or f["acodec"].startswith("mp4a")]
    else:
        pref = [f for f in auds if f["ext"] == "webm" or "opus" in f["acodec"]]
    auds = sorted(pref or auds, key=lambda f: f["abr"] or f["tbr"])
    if not auds:
        return None
    if quality == "low":
        return auds[0]
    if quality == "high":
        return auds[-1]
    mid = [f for f in auds if (f["abr"] or f["tbr"]) <= 135]
    return mid[-1] if mid else auds[0]


def pick_video(info: dict, height: int = 480) -> dict | None:
    vids = [f for f in info["formats"] if f["vcodec"] != "none" and f["acodec"] == "none" and f["height"]]
    avc = [f for f in vids if f["vcodec"].startswith("avc1") and f["ext"] == "mp4"]
    vids = avc or vids
    if not vids:  # muxed fallback (itag 18)
        vids = [f for f in info["formats"] if f["vcodec"] != "none" and f["height"]]
    under = sorted([f for f in vids if f["height"] <= height], key=lambda f: (f["height"], f["tbr"]))
    if under:
        return under[-1]
    return min(vids, key=lambda f: f["height"]) if vids else None


def video_heights(info: dict) -> list[int]:
    hs = {f["height"] for f in info["formats"] if f["vcodec"] != "none" and f["height"]}
    return sorted(h for h in hs if h in (144, 240, 360, 480, 720, 1080))


# ---- audio cache ---------------------------------------------------------------------------
def _cache_path(vid: str, fid: str) -> str:
    safe = re.sub(r"[^\w-]", "", str(fid))
    return os.path.join(CACHE_DIR, f"{vid}_{safe}.bin")


async def _download(vid: str, fmt: dict):
    path = _cache_path(vid, fmt["id"])
    tmp = path + ".part"
    try:
        pos = 0
        with open(tmp, "wb") as fh:
            while True:
                r = await http().get(fmt["url"], headers={**fmt["headers"], "Range": f"bytes={pos}-{pos + CHUNK - 1}"})
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"status {r.status_code}")
                fh.write(r.content)
                total = int(r.headers.get("content-range", "/0").split("/")[-1] or 0)
                pos += len(r.content)
                if r.status_code == 200 or not r.content or (total and pos >= total):
                    break
        os.replace(tmp, path)
        _trim_cache()
        spawn_analyze(vid, path)
    except Exception as e:
        log.info("cache download failed %s: %s", vid, e)
        if os.path.exists(tmp):
            os.remove(tmp)


def _trim_cache():
    files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".bin")]
    files.sort(key=os.path.getmtime)
    total = sum(os.path.getsize(f) for f in files)
    while files and total > config.AUDIO_CACHE_MB * 1024 * 1024:
        f = files.pop(0)
        total -= os.path.getsize(f)
        os.remove(f)


async def prefetch(vid: str):
    """Resolve + cache the default audio formats for a track (both codecs) in the background."""
    try:
        info = await resolve(vid)
    except Exception as e:
        log.info("prefetch resolve failed %s: %s", vid, e)
        return
    for codec in ("aac", "opus"):
        f = pick_audio(info, codec, "medium")
        if not f:
            continue
        key = f"{vid}_{f['id']}"
        if os.path.exists(_cache_path(vid, f["id"])):
            spawn_analyze(vid, _cache_path(vid, f["id"]))
            continue
        if key in _downloads:
            continue
        t = asyncio.create_task(_download(vid, f))
        _downloads[key] = t
        t.add_done_callback(lambda _t, k=key: _downloads.pop(k, None))


# ---- silence at the start / end of songs -----------------------------------------------------------------
# Many uploads open with a second or more of silence (or end with a long quiet tail). Once a song is in the cache,
# ffmpeg finds those parts; the room then starts the song where the sound starts and moves on where it ends.
_trims: dict[str, tuple[float, float | None]] = {}  # vid -> (start, end or None)
_analyzing: set[str] = set()


def trim(vid: str) -> tuple[float, float | None] | None:
    return _trims.get(vid)


def trim_pending(vid: str) -> bool:
    """The song is downloading or being checked right now (its silence info is coming soon)."""
    return vid in _analyzing or any(k.startswith(vid + "_") for k in _downloads)


def spawn_analyze(vid: str, path: str):
    if vid in _trims or vid in _analyzing:
        return
    _analyzing.add(vid)

    async def run():
        try:
            _trims[vid] = await asyncio.to_thread(_silence, path)
            if len(_trims) > 3000:
                for k in list(_trims)[:500]:
                    _trims.pop(k, None)
        except Exception as e:
            log.info("silence check failed %s: %s", vid, e)
        finally:
            _analyzing.discard(vid)
    asyncio.get_running_loop().create_task(run())


def _silence(path: str) -> tuple[float, float | None]:
    import subprocess
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "silencedetect=noise=-50dB:d=0.4",
                       "-f", "null", "-"], capture_output=True, text=True, timeout=120)
    return parse_silence(p.stderr)


def parse_silence(log_text: str) -> tuple[float, float | None]:
    """(where the sound starts, where it ends or None) from ffmpeg silencedetect output."""
    dur = None
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", log_text)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.findall(r"time=(\d+):(\d+):([\d.]+)", log_text)  # what was really decoded (Duration can be missing)
    if m:
        h, mi, se = m[-1]
        dur = max(dur or 0, int(h) * 3600 + int(mi) * 60 + float(se))
    spans, open_at = [], None
    for kind, val in re.findall(r"silence_(start|end): (-?[\d.]+)", log_text):
        if kind == "start":
            open_at = float(val)
        elif open_at is not None:
            spans.append((open_at, float(val)))
            open_at = None
    if open_at is not None and dur:
        spans.append((open_at, dur))  # silent right to the end
    start, end = 0.0, None
    if spans and spans[0][0] <= 0.05:
        start = min(spans[0][1], 15.0)  # never skip more than 15 s (a quiet intro isn't silence)
    if dur and spans and spans[-1][1] >= dur - 0.3 and dur - spans[-1][0] < 40 and spans[-1][0] > start + 10:
        end = spans[-1][0] + 0.3  # keep a breath of the tail
    return round(max(0.0, start - 0.05), 2), round(end, 2) if end else None


def _mime(fmt: dict) -> str:
    if fmt["vcodec"] == "none":
        return "audio/mp4" if fmt["ext"] == "m4a" else "audio/webm"
    return "video/mp4" if fmt["ext"] == "mp4" else "video/webm"


async def stream(request: Request, vid: str, kind: str, codec: str, quality: str) -> Response:
    try:
        info = await resolve(vid)
    except Exception as e:
        log.warning("resolve failed %s: %s", vid, e)
        return Response("unavailable", status_code=502)
    if kind == "audio":
        fmt = pick_audio(info, codec, quality)
    else:
        fmt = pick_video(info, int(quality) if quality.isdigit() else 480)
    if not fmt:
        return Response("no format", status_code=404)
    path = _cache_path(vid, fmt["id"])
    if os.path.exists(path):
        try:
            os.utime(path)  # LRU: mark as recently used (atime is often not updated on volumes)
        except OSError:
            pass
        return FileResponse(path, media_type=_mime(fmt), headers={"Cache-Control": "private, max-age=3600"})
    try:
        return await _proxy(request, vid, fmt, retry=True)
    except Exception as e:
        log.warning("stream proxy failed %s: %s", vid, e)
        return Response("upstream error", status_code=502)


async def _proxy(request: Request, vid: str, fmt: dict, retry: bool) -> Response:
    rng = request.headers.get("range", "bytes=0-")
    m = re.match(r"bytes=(\d*)-(\d*)", rng)
    start = int(m.group(1) or 0) if m else 0
    end = int(m.group(2)) if m and m.group(2) else start + CHUNK - 1
    end = min(end, start + CHUNK - 1)
    req = http().build_request("GET", fmt["url"], headers={**fmt["headers"], "Range": f"bytes={start}-{end}"})
    r = await http().send(req, stream=True)
    if r.status_code in (403, 410) and retry:
        await r.aclose()
        info = await resolve(vid, force=True)
        same = [f for f in info["formats"] if f["id"] == fmt["id"]]
        if same:
            return await _proxy(request, vid, same[0], retry=False)
        return Response("expired", status_code=502)
    if r.status_code not in (200, 206):
        await r.aclose()
        return Response("upstream error", status_code=502)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    for h in ("content-length", "content-range"):
        if h in r.headers:
            headers[h] = r.headers[h]

    async def body():
        try:
            async for chunk in r.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await r.aclose()

    return StreamingResponse(body(), status_code=r.status_code, media_type=_mime(fmt), headers=headers)


_thumbs: "OrderedDict[str, bytes | None]" = OrderedDict()  # small in-memory LRU so ytimg isn't hit on every view


async def thumb(vid: str) -> Response:
    if not valid_id(vid):
        return Response(status_code=404)
    if vid in _thumbs:
        _thumbs.move_to_end(vid)
        data = _thumbs[vid]
    else:
        data, failed = None, False
        for q in ("maxresdefault", "hqdefault", "mqdefault"):
            try:
                r = await http().get(f"https://i.ytimg.com/vi/{vid}/{q}.jpg")
                if r.status_code == 200 and len(r.content) > 2000:
                    data = r.content
                    break
            except Exception:
                failed = True
        if data or not failed:  # don't remember network errors as "no thumbnail"
            _thumbs[vid] = data
            while len(_thumbs) > 300:
                _thumbs.popitem(last=False)
    if not data:
        return Response(status_code=404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


# ---- lyrics (LRCLIB, free) -----------------------------------------------------------------
_JUNK = re.compile(r"[\(\[][^\)\]]*(official|video|audio|lyric|lyrics|visualizer|hd|4k|remaster|full song|mv)[^\)\]]*[\)\]]", re.I)
_lyr_cache: "OrderedDict[str, dict]" = OrderedDict()


def _clean(title: str, artist: str) -> tuple[str, str]:
    t = _JUNK.sub("", title)
    t = re.sub(r"\s*\|.*$", "", t)
    t = re.sub(r"\s*(ft\.?|feat\.?)\s.*$", "", t, flags=re.I).strip(" -")
    if " - " in t:
        a, b = t.split(" - ", 1)
        return b.strip(), a.strip()
    return t.strip(), artist


async def lyrics(vid: str, title: str, artist: str, duration: float) -> dict:
    if vid in _lyr_cache:
        _lyr_cache.move_to_end(vid)
        return _lyr_cache[vid]
    name, art = _clean(title, artist)
    res = {"synced": None, "plain": None}
    try:
        r = await http().get("https://lrclib.net/api/search", params={"track_name": name, "artist_name": art},
                             headers={"User-Agent": "VCMusicMiniApp/1.0"})
        items = r.json() if r.status_code == 200 else []
        if not items:
            r = await http().get("https://lrclib.net/api/search", params={"q": f"{art} {name}"},
                                 headers={"User-Agent": "VCMusicMiniApp/1.0"})
            items = r.json() if r.status_code == 200 else []
        if items:
            items.sort(key=lambda i: (not i.get("syncedLyrics"), abs((i.get("duration") or 0) - duration)))
            best = items[0]
            if not duration or abs((best.get("duration") or 0) - duration) < 15:
                res = {"synced": best.get("syncedLyrics"), "plain": best.get("plainLyrics")}
    except Exception as e:
        log.info("lyrics failed: %s", e)
        return res  # don't cache failures: try again next time
    _lyr_cache[vid] = res
    while len(_lyr_cache) > 500:
        _lyr_cache.popitem(last=False)
    return res


# ---- browse: genres, trending, recommendations (cached so YouTube isn't hit every open) ----------
YEAR = time.strftime("%Y")
GENRES = [
    {"id": "punjabi", "title": "Punjabi", "c": ["#ff7a18", "#af002d"], "q": [f"latest punjabi songs {YEAR}", "punjabi hit songs"]},
    {"id": "bollywood", "title": "Bollywood", "c": ["#f7971e", "#d4145a"], "q": [f"latest bollywood songs {YEAR}", "bollywood hits"]},
    {"id": "hiphop", "title": "Hip-Hop & Rap", "c": ["#232526", "#5b5b5b"], "q": ["desi hip hop songs", "hip hop hits"]},
    {"id": "romantic", "title": "Romantic", "c": ["#ee0979", "#ff6a88"], "q": ["romantic hindi songs", "romantic punjabi songs"]},
    {"id": "party", "title": "Party", "c": ["#8e2de2", "#4a00e0"], "q": ["party songs hindi punjabi", "dance party hits"]},
    {"id": "sad", "title": "Sad", "c": ["#2c3e50", "#4ca1af"], "q": ["sad hindi songs", "sad punjabi songs"]},
    {"id": "lofi", "title": "Lo-fi & Chill", "c": ["#614385", "#516395"], "q": ["hindi lofi songs", "lofi chill songs"]},
    {"id": "pop", "title": "Pop", "c": ["#fc466b", "#3f5efb"], "q": [f"pop hits {YEAR}", "english pop songs"]},
    {"id": "haryanvi", "title": "Haryanvi", "c": ["#11998e", "#38ef7d"], "q": [f"latest haryanvi songs {YEAR}", "haryanvi hit songs"]},
    {"id": "retro", "title": "90s & Retro", "c": ["#c79081", "#dfa579"], "q": ["90s hindi songs", "old hindi songs evergreen"]},
    {"id": "workout", "title": "Workout", "c": ["#f12711", "#f5af19"], "q": ["gym workout songs", "workout motivation songs"]},
    {"id": "edm", "title": "Dance & EDM", "c": ["#00c6ff", "#0072ff"], "q": ["edm hits", "bollywood remix songs"]},
    {"id": "indie", "title": "Indie", "c": ["#606c88", "#3f4c6b"], "q": ["hindi indie songs", "indie pop songs india"]},
    {"id": "devotional", "title": "Devotional", "c": ["#f6d365", "#fda085"], "q": ["shabad gurbani kirtan", "bhakti songs"]},
    {"id": "english", "title": "English Hits", "c": ["#1d2b64", "#f8cdda"], "q": [f"top english songs {YEAR}", "billboard hits"]},
    {"id": "sufi", "title": "Sufi & Ghazal", "c": ["#3a1c71", "#d76d77"], "q": ["sufi songs", "best ghazals"]},
]
SPECIAL = {
    "trending": [f"trending songs india {YEAR}", f"top hits this week {YEAR}"],
    "new": [f"new songs this week {YEAR}", f"new punjabi songs {YEAR}"],
}
_bcache: dict[str, tuple[float, list]] = {}
_blocks: dict[str, asyncio.Lock] = {}


async def _cached(key: str, ttl: float, fn):
    hit = _bcache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    lock = _blocks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _bcache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = await fn()
        except Exception as e:
            log.info("browse %s failed: %s", key, e)
            if hit:
                return hit[1]
            raise
        _bcache[key] = (time.time(), val)
        if len(_bcache) > 200:  # recommendation keys are per seed set; drop the oldest
            for k, _ in sorted(_bcache.items(), key=lambda kv: kv[1][0])[:50]:
                _bcache.pop(k, None)
                _blocks.pop(k, None)
        return val


def _interleave(lists: list[list[dict]], limit: int) -> list[dict]:
    out, seen = [], set()
    for i in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if i < len(lst) and lst[i]["id"] not in seen and (not lst[i]["duration"] or 60 < lst[i]["duration"] < 900):
                seen.add(lst[i]["id"])
                out.append(lst[i])
    return out[:limit]


async def _multi(queries: list[str], limit: int) -> list[dict]:
    res = await asyncio.gather(*(search(q, 15) for q in queries), return_exceptions=True)
    return _interleave([r for r in res if isinstance(r, list)], limit)


def genre_list() -> list[dict]:
    return [{"id": g["id"], "title": g["title"], "c": g["c"]} for g in GENRES]


async def collection(cid: str) -> dict:
    g = next((x for x in GENRES if x["id"] == cid), None)
    if g:
        tracks = await _cached("g:" + cid, 6 * 3600, lambda: _multi(g["q"], 30))
        return {"title": g["title"], "c": g["c"], "tracks": tracks}
    if cid in SPECIAL:
        title = {"trending": "Trending Now", "new": "New Releases"}[cid]
        tracks = await _cached("s:" + cid, 3 * 3600, lambda: _multi(SPECIAL[cid], 30))
        return {"title": title, "c": ["#fa2d48", "#a8183a"] if cid == "trending" else ["#5e5ce6", "#bf5af2"], "tracks": tracks}
    raise KeyError(cid)


async def recommend(seed_ids: list[str], exclude: set[str]) -> list[dict]:
    seeds = seed_ids[:3]
    if not seeds:
        return []
    key = "r:" + ",".join(seeds)

    async def build():
        res = await asyncio.gather(*(related(s, 20) for s in seeds), return_exceptions=True)
        return _interleave([r for r in res if isinstance(r, list)], 40)

    tracks = await _cached(key, 1800, build)
    return [t for t in tracks if t["id"] not in exclude][:25]
