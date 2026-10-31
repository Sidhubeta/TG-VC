import hashlib
import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


BOT_TOKEN = _env("BOT_TOKEN")
# Short name of the Mini App created in @BotFather (/newapp). Link: t.me/<bot>/<APP_SHORT_NAME>
APP_SHORT_NAME = _env("APP_SHORT_NAME", "vc")
PUBLIC_URL = _env("PUBLIC_URL").rstrip("/")  # https://your-app.up.railway.app
PORT = int(_env("PORT", "8000"))
DATA_DIR = _env("DATA_DIR", "./data")
SECRET_KEY = _env("SECRET_KEY") or hashlib.sha256(("vc-secret:" + BOT_TOKEN).encode()).hexdigest()

# LiveKit (voice / video). LIVEKIT_URL is the public wss:// URL the browser connects to.
LIVEKIT_URL = _env("LIVEKIT_URL")
LIVEKIT_API_KEY = _env("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = _env("LIVEKIT_API_SECRET")
# URL the backend uses for the server API; defaults to LIVEKIT_URL with https://
LIVEKIT_HTTP_URL = (_env("LIVEKIT_HTTP_URL") or LIVEKIT_URL.replace("wss://", "https://").replace("ws://", "http://")).rstrip("/")

# YouTube
YT_COOKIES = _env("YT_COOKIES")            # contents of cookies.txt (Netscape format), optional
YT_COOKIES_B64 = _env("YT_COOKIES_B64")    # same, base64 encoded (easier for Railway vars)
POT_PROVIDER_URL = _env("POT_PROVIDER_URL")  # bgutil PO token provider, e.g. http://bgutil.railway.internal:4416
AUDIO_CACHE_MB = int(_env("AUDIO_CACHE_MB", "800"))
VIDEO_CACHE_MB = int(_env("VIDEO_CACHE_MB", "600"))  # 0 = don't cache videos
# The bot's owner (your Telegram id): sees the Health page and adds sudo users (/addsudo, or on the Health page)
_owner = _env("OWNER_ID").strip()
OWNER_ID = int(_owner) if _owner.isdigit() else 0

# Optional: the full member list for invites. The Bot API can't list a group's members, Telegram's full API
# (MTProto) can. Get both values at https://my.telegram.org -> API development tools. Without them, the invite
# list shows the members the bot has seen.
TG_API_ID = _env("TG_API_ID")
TG_API_HASH = _env("TG_API_HASH")

# Optional: Spotify links. Without these, playlists/albums are read from Spotify's public embed page (up to ~100
# songs); with a free app from https://developer.spotify.com the whole playlist is read.
SPOTIFY_CLIENT_ID = _env("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = _env("SPOTIFY_CLIENT_SECRET")

# Time zone for badges like "Night owl" (people's local time)
TIMEZONE = _env("TIMEZONE", "Asia/Kolkata")

# Open the Mini App in fullscreen from group links (Telegram 8.0+; older apps just open normally)
FULLSCREEN = _env("FULLSCREEN", "1") == "1"

# Local testing only: lets you open the app in a normal browser with a fake user. NEVER enable in production.
DEV_MODE = _env("DEV_MODE") == "1"

os.makedirs(DATA_DIR, exist_ok=True)
