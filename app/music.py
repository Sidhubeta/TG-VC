"""YouTube via yt-dlp: search, playlists, stream resolving, audio cache, range proxy, lyrics."""
import asyncio
import base64
import logging
import os
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
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

# yt-dlp loads its plugins (the PO-token provider YouTube needs) the first time a YoutubeDL is created, and that
# check isn't thread-safe. Two songs resolved at once in two threads loaded the plugin twice ("BgUtilHTTP already
# registered"), and that lookup went without a PO token: YouTube answered 403 to its stream links. Load them once,
# here, before any thread runs.
POT_PLUGIN = False
if yt_dlp is not None:
    try:
        from yt_dlp.plugins import all_plugins_loaded, load_all_plugins
        if not all_plugins_loaded.value:
            load_all_plugins()
        import importlib.util
        POT_PLUGIN = importlib.util.find_spec("yt_dlp_plugins.extractor.getpot_bgutil_http") is not None
    except Exception as e:  # pragma: no cover
        log.warning("yt-dlp plugins failed to load: %s", e)

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


_sthttp: httpx.AsyncClient | None = None


def stream_http() -> httpx.AsyncClient:
    """Song streams and downloads. Each listener's stream holds a connection for the whole song; on the shared
    client (100 connections) a big room used up the pool and thumbnails, lyrics and new streams timed out."""
    global _sthttp
    if _sthttp is None:
        _sthttp = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10, pool=20), follow_redirects=True,
                                    limits=httpx.Limits(max_connections=600, max_keepalive_connections=60))
    return _sthttp


# yt-dlp and ffmpeg work (seconds each) runs on its own threads, so it can't hold up anything else
_POOL = ThreadPoolExecutor(8, thread_name_prefix="yt")


def _bg(fn, *args, **kw):
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(_POOL, lambda: fn(*args, **kw))


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
    # playlists list removed and private uploads too ("[Private video]", "[Deleted video]"): they can't be played,
    # and with no length the room used to wait on them forever
    if e.get("availability") in ("private", "needs_auth", "premium_only", "subscriber_only"):
        return None
    if re.fullmatch(r"\[(private|deleted|unavailable) video\]", (e.get("title") or "").strip().lower()):
        return None
    artist = e.get("artist") or e.get("channel") or e.get("uploader") or ""
    artist = re.sub(r"\s*-\s*Topic$", "", artist)
    try:
        dur = float(e.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return {"id": vid, "title": e.get("track") or e.get("title") or "Unknown", "artist": artist,
            "duration": dur if 0 < dur < 86400 else 0.0, "thumb": _thumb(vid)}


def looks_like_id(text: str) -> bool:
    """A bare 11-letter text is taken as a video id only if it looks random: "Cheerleader" or "Bohemianish" are
    words to search for. Real ids almost always have a digit, - or _, or capitals after the first letter."""
    return bool(VID_RE.fullmatch(text)) and bool(re.search(r"[\d_-]", text) or re.search(r"[A-Z]", text[1:]))


def parse_youtube(text: str) -> tuple[str | None, str | None]:
    """Returns (video_id, playlist_id) from any YouTube / YT Music URL."""
    text = text.strip()
    if re.fullmatch(r"[\w-]{11}", text):
        return (text, None) if looks_like_id(text) else (None, None)
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


# ---- SoundCloud links: same idea as Spotify (song names from SoundCloud, the songs themselves from YouTube) ----
SC_RE = re.compile(r"https?://(?:on\.|m\.|www\.)?soundcloud\.com/[^\s]+", re.I)


def parse_soundcloud(text: str) -> str | None:
    m = SC_RE.search(text or "")
    return m.group(0).split("?")[0] if m else None


def _sc_song(e: dict) -> dict:
    title = (e.get("title") or "").strip()
    artist = (e.get("artist") or e.get("uploader") or "").strip()
    if " - " in title and not e.get("artist"):  # "Artist - Song" is the usual SoundCloud way of naming uploads
        artist, title = [x.strip() for x in title.split(" - ", 1)]
    return {"title": title, "artist": artist, "duration": round(e.get("duration") or 0)}


async def soundcloud(url: str, limit: int = 100) -> tuple[str, list[dict]]:
    """(name, [{title, artist, duration}]) for a SoundCloud song, playlist / album or profile."""
    if "on.soundcloud.com" in url:  # short share link: find where it points
        r = await http().get(url, headers={"User-Agent": "Mozilla/5.0"})
        url = str(r.url).split("?")[0]
        if "soundcloud.com" not in url:
            raise ValueError("SoundCloud link not understood")
    info = await _bg(_extract, url, extract_flat="in_playlist", noplaylist=False, playlistend=limit)
    if info.get("_type") != "playlist" and not info.get("entries"):
        return info.get("title") or "SoundCloud", [_sc_song(info)]
    entries = [e for e in info.get("entries") or [] if e][:limit]
    sem = asyncio.Semaphore(4)

    async def full(e):  # a playlist may list only links: look those songs up one by one
        if e.get("title"):
            return _sc_song(e)
        async with sem:
            try:
                return _sc_song(await _bg(_extract, e.get("url") or e.get("webpage_url")))
            except Exception:
                return None
    songs = [x for x in await asyncio.gather(*(full(e) for e in entries)) if x and x["title"]]
    return info.get("title") or "SoundCloud", songs


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


_searches: "OrderedDict[tuple, tuple[float, list[dict]]]" = OrderedDict()


async def search(query: str, limit: int = 15) -> list[dict]:
    """YouTube search. Results are kept 10 minutes: people type a word a letter at a time and search the same
    things again, and every search sent to YouTube adds to the chance it starts blocking the server."""
    key = (" ".join(query.lower().split()), limit)
    hit = _searches.get(key)
    if hit and time.time() - hit[0] < 600:
        _searches.move_to_end(key)
        return hit[1]
    info = await _bg(_extract, f"ytsearch{limit}:{query}", extract_flat="in_playlist")
    res = [t for e in info.get("entries") or [] if (t := _track(e))]
    _searches[key] = (time.time(), res)
    while len(_searches) > 500:
        _searches.popitem(last=False)
    return res


async def playlist(pid: str, limit: int = 200) -> tuple[str, list[dict]]:
    info = await _bg(_extract, f"https://www.youtube.com/playlist?list={pid}",
                                   extract_flat="in_playlist", noplaylist=False, playlistend=limit)
    return info.get("title") or "Playlist", [t for e in info.get("entries") or [] if (t := _track(e))]


async def related(vid: str, limit: int = 25) -> list[dict]:
    info = await _bg(_extract, f"https://www.youtube.com/watch?v={vid}&list=RD{vid}",
                                   extract_flat="in_playlist", noplaylist=False, playlistend=limit)
    return [t for e in info.get("entries") or [] if (t := _track(e)) and t["id"] != vid]


_DEAD = re.compile(r"private video|video unavailable|has been removed|no longer available|is not available|"
                   r"not available in your country|members[- ]only|join this channel|confirm your age|age[- ]restricted|"
                   r"copyright|terminated|does not exist|premieres in|live event will begin", re.I)


def unplayable(err) -> bool:
    """The song itself can't be played (private, removed, blocked, age-restricted) — as opposed to YouTube
    blocking the server for a while ("confirm you're not a bot", 403s, timeouts), where skipping would empty the
    whole queue in a few seconds."""
    return bool(_DEAD.search(str(err))) and "not a bot" not in str(err)


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
    asked = time.time()
    async with lock:
        hit = _info_cache.get(vid)
        if hit and not force and time.time() - hit[0] < 3 * 3600:
            return hit[1]
        # A forced refresh (a link was refused) that someone else already did while we waited is good enough:
        # 20 listeners refused at once used to run 20 YouTube lookups one after another.
        if hit and force and hit[0] >= asked:
            return hit[1]
        try:
            info = await _bg(_extract, f"https://www.youtube.com/watch?v={vid}")
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


async def _download(vid: str, fmt: dict, retry: bool = True):
    path = _cache_path(vid, fmt["id"])
    tmp = path + ".part"
    try:
        pos = 0
        with open(tmp, "wb") as fh:
            while True:
                r = await stream_http().get(fmt["url"], headers={**fmt["headers"], "Range": f"bytes={pos}-{pos + CHUNK - 1}"})
                if r.status_code in (403, 410) and retry:
                    raise _Expired()
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
    except _Expired:  # the link went stale (or came without a PO token): get a fresh one and try once more
        if os.path.exists(tmp):
            os.remove(tmp)
        try:
            info = await resolve(vid, force=True)
            same = [f for f in info["formats"] if f["id"] == fmt["id"]]
            if same:
                return await _download(vid, same[0], retry=False)
        except Exception as e:
            log.info("cache download retry failed %s: %s", vid, e)
    except Exception as e:
        log.info("cache download failed %s: %s", vid, e)
        if os.path.exists(tmp):
            os.remove(tmp)


class _Expired(Exception):
    pass


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
        _start_download(vid, f)


def _start_download(vid: str, f: dict) -> asyncio.Task:
    key = f"{vid}_{f['id']}"
    t = _downloads.get(key)
    if t is None:
        t = _downloads[key] = asyncio.get_running_loop().create_task(_download(vid, f))
        t.add_done_callback(lambda _t, k=key: _downloads.pop(k, None))
    return t


_served_as: dict[tuple[str, str], str] = {}  # (song, format asked for) -> format whose file is served


def _cached_alternative(info: dict, vid: str, fmt: dict) -> dict | None:
    """Another quality of the same kind of audio that's already on disk (the cache holds "medium")."""
    opus = "opus" in fmt["acodec"] or fmt["ext"] == "webm"
    same = [f for f in info["formats"] if f["vcodec"] == "none" and f["acodec"] != "none"
            and ("opus" in f["acodec"] or f["ext"] == "webm") == opus and os.path.exists(_cache_path(vid, f["id"]))]
    want = fmt["abr"] or fmt["tbr"] or 0
    return min(same, key=lambda f: abs((f["abr"] or f["tbr"] or 0) - want)) if same else None


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
            _trims[vid] = await _bg(_silence, path)
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
    key = (vid, fmt["id"])
    if kind == "audio" and key in _served_as:
        sub = [f for f in info["formats"] if f["id"] == _served_as[key]]
        if sub and os.path.exists(_cache_path(vid, sub[0]["id"])):
            fmt, path = sub[0], _cache_path(vid, sub[0]["id"])
    elif kind == "audio" and not os.path.exists(path):
        # Not on disk yet. Every listener used to pull the song from YouTube on their own until the whole file was
        # cached — many requests from one server address, the main reason for 403s and throttling. Use another
        # quality that's already cached, or wait a moment for the download that's running (one request for all).
        # The choice is remembered: a player asks for a file in pieces, and every piece must come from the same file.
        alt = _cached_alternative(info, vid, fmt)
        if alt:
            _served_as[key] = alt["id"]
            fmt, path = alt, _cache_path(vid, alt["id"])
        else:
            _served_as[key] = fmt["id"]
            try:
                await asyncio.wait_for(asyncio.shield(_start_download(vid, fmt)), 6)
            except Exception:
                pass
        while len(_served_as) > 3000:
            _served_as.pop(next(iter(_served_as)))
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
    req = stream_http().build_request("GET", fmt["url"], headers={**fmt["headers"], "Range": f"bytes={start}-{end}"})
    r = await stream_http().send(req, stream=True)
    if r.status_code in (403, 410) and retry:
        await r.aclose()
        info = await resolve(vid, force=True)
        same = [f for f in info["formats"] if f["id"] == fmt["id"]]
        if not same:  # that format is gone: the closest one of the same kind
            kind = "video" if fmt["vcodec"] != "none" else "audio"
            same = [f for f in info["formats"] if (f["vcodec"] != "none") == (kind == "video") and f["acodec"] == fmt["acodec"]]
        if same:
            return await _proxy(request, vid, same[0], retry=False)
        return Response("expired", status_code=503, headers={"Retry-After": "1"})
    if r.status_code not in (200, 206):
        await r.aclose()
        log.info("stream %s: YouTube answered %s", vid, r.status_code)
        STATS["yt_err"], STATS["yt_err_at"] = f"stream answered {r.status_code}", time.time()
        return Response("upstream error", status_code=503, headers={"Retry-After": "1"})
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


# ---- the song in short pieces, for Web Audio --------------------------------------------------------------------
# Telegram on Android won't let an <audio> element make a sound before the first tap, but Web Audio may play right
# away. Phones that are held back fetch the song as short Ogg Opus pieces, decode them and play them on the room's
# clock. Pieces sit on a fixed 10 s grid (and run a little past it, so the player can blend one into the next), so
# everyone held back shares them.
PIECE = 10.0
PIECE_OVERLAP = 0.3
PIECE_KBPS = {"low": 64, "medium": 128, "high": 160}
_pieces: "OrderedDict[tuple, bytes]" = OrderedDict()
_piece_jobs: dict[tuple, asyncio.Task] = {}
_piece_sem = asyncio.Semaphore(3)


def _piece_source(info: dict, vid: str) -> tuple[str, dict]:
    """The cached file if there is one (no YouTube request), else the stream link."""
    fmts = [pick_audio(info, c, q) for c in ("opus", "aac") for q in ("medium", "high", "low")]
    for f in fmts:
        if f and os.path.exists(_cache_path(vid, f["id"])):
            return _cache_path(vid, f["id"]), {}
    f = next((f for f in fmts if f), None)
    if not f:
        raise LookupError("no audio")
    return f["url"], f["headers"]


async def _encode_piece(src: str, headers: dict, at: float, kbps: int) -> bytes:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    args += ["-ss", f"{at:.3f}", "-i", src, "-t", f"{PIECE + PIECE_OVERLAP:.3f}", "-vn", "-ac", "2", "-ar", "48000",
             "-c:a", "libopus", "-b:a", f"{kbps}k", "-f", "ogg", "pipe:1"]
    async with _piece_sem:
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(p.communicate(), 25)
        except asyncio.TimeoutError:
            p.kill()
            raise RuntimeError("ffmpeg timed out")
    if p.returncode != 0:
        raise RuntimeError(err.decode(errors="replace")[-300:])
    return out


async def piece(vid: str, k: int, quality: str) -> Response:
    if not 0 <= k < 2000:
        return Response("bad piece", status_code=400)
    key = (vid, k, quality if quality in PIECE_KBPS else "medium")
    data = _pieces.get(key)
    if data is None:
        job = _piece_jobs.get(key)
        if not job:
            async def make():
                info = await resolve(vid)
                src, headers = _piece_source(info, vid)
                try:
                    return await _encode_piece(src, headers, k * PIECE, PIECE_KBPS[key[2]])
                except RuntimeError:
                    if not src.startswith("http"):
                        raise
                    info = await resolve(vid, force=True)  # the link may have gone stale: once more with a fresh one
                    src, headers = _piece_source(info, vid)
                    return await _encode_piece(src, headers, k * PIECE, PIECE_KBPS[key[2]])
            job = _piece_jobs[key] = asyncio.get_running_loop().create_task(make())
            job.add_done_callback(lambda _t: _piece_jobs.pop(key, None))
        try:
            data = await asyncio.shield(job)
        except Exception as e:
            log.info("piece %s/%s failed: %s", vid, k, str(e)[:200])
            return Response("unavailable", status_code=503, headers={"Retry-After": "1"})
        _pieces[key] = data
        while len(_pieces) > 60:  # ~10 MB of pieces at most
            _pieces.popitem(last=False)
    else:
        _pieces.move_to_end(key)
    # an empty piece means the song is over
    return Response(data, media_type="audio/ogg", headers={"Cache-Control": "private, max-age=3600"})


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
            # by size, not count: big covers are ~150 KB each, 300 of them took ~45 MB
            total = sum(len(v or b"") for v in _thumbs.values())
            while _thumbs and (total > 16 * 1024 * 1024 or len(_thumbs) > 600):
                _, old = _thumbs.popitem(last=False)
                total -= len(old or b"")
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
    # Kept per song AND the name asked for: the name comes from the phone, so one person sending a wrong name
    # could otherwise have given everyone the wrong lyrics for that song.
    key = f"{vid}|{title.lower()}|{artist.lower()}"
    if key in _lyr_cache:
        _lyr_cache.move_to_end(key)
        return _lyr_cache[key]
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
    _lyr_cache[key] = res
    while len(_lyr_cache) > 500:
        _lyr_cache.popitem(last=False)
    return res


# ---- browse: genres, trending, recommendations (cached so YouTube isn't hit every open) ----------
# "{year}" is filled in when a search runs (built once at start-up, a server running across New Year kept
# searching last year's hits)
GENRES = [
    {"id": "punjabi", "title": "Punjabi", "c": ["#ff7a18", "#af002d"], "q": ["latest punjabi songs {year}", "punjabi hit songs"]},
    {"id": "bollywood", "title": "Bollywood", "c": ["#f7971e", "#d4145a"], "q": ["latest bollywood songs {year}", "bollywood hits"]},
    {"id": "hiphop", "title": "Hip-Hop & Rap", "c": ["#232526", "#5b5b5b"], "q": ["desi hip hop songs", "hip hop hits"]},
    {"id": "romantic", "title": "Romantic", "c": ["#ee0979", "#ff6a88"], "q": ["romantic hindi songs", "romantic punjabi songs"]},
    {"id": "party", "title": "Party", "c": ["#8e2de2", "#4a00e0"], "q": ["party songs hindi punjabi", "dance party hits"]},
    {"id": "sad", "title": "Sad", "c": ["#2c3e50", "#4ca1af"], "q": ["sad hindi songs", "sad punjabi songs"]},
    {"id": "lofi", "title": "Lo-fi & Chill", "c": ["#614385", "#516395"], "q": ["hindi lofi songs", "lofi chill songs"]},
    {"id": "pop", "title": "Pop", "c": ["#fc466b", "#3f5efb"], "q": ["pop hits {year}", "english pop songs"]},
    {"id": "haryanvi", "title": "Haryanvi", "c": ["#11998e", "#38ef7d"], "q": ["latest haryanvi songs {year}", "haryanvi hit songs"]},
    {"id": "retro", "title": "90s & Retro", "c": ["#c79081", "#dfa579"], "q": ["90s hindi songs", "old hindi songs evergreen"]},
    {"id": "workout", "title": "Workout", "c": ["#f12711", "#f5af19"], "q": ["gym workout songs", "workout motivation songs"]},
    {"id": "edm", "title": "Dance & EDM", "c": ["#00c6ff", "#0072ff"], "q": ["edm hits", "bollywood remix songs"]},
    {"id": "indie", "title": "Indie", "c": ["#606c88", "#3f4c6b"], "q": ["hindi indie songs", "indie pop songs india"]},
    {"id": "devotional", "title": "Devotional", "c": ["#f6d365", "#fda085"], "q": ["shabad gurbani kirtan", "bhakti songs"]},
    {"id": "english", "title": "English Hits", "c": ["#1d2b64", "#f8cdda"], "q": ["top english songs {year}", "billboard hits"]},
    {"id": "sufi", "title": "Sufi & Ghazal", "c": ["#3a1c71", "#d76d77"], "q": ["sufi songs", "best ghazals"]},
]
SPECIAL = {
    "trending": ["trending songs india {year}", "top hits this week {year}"],
    "new": ["new songs this week {year}", "new punjabi songs {year}"],
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
    year = time.strftime("%Y")
    res = await asyncio.gather(*(search(q.format(year=year), 15) for q in queries), return_exceptions=True)
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
