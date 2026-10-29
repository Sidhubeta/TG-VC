import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from . import config


def _hmac(data: str, n: int = 32) -> str:
    return hmac.new(config.SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()[:n]


# ---- Telegram WebApp initData -----------------------------------------------------------
def verify_init_data(init_data: str, max_age: int = 86400) -> dict:
    """Validate Telegram Mini App initData. Raises ValueError if forged or expired."""
    data_with_sig = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received = data_with_sig.pop("hash", None)
    if not received or not config.BOT_TOKEN:
        raise ValueError("missing hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data_with_sig.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise ValueError("bad hash")
    try:
        auth_date = int(data_with_sig.get("auth_date", "0"))
        user = json.loads(data_with_sig.get("user", "{}"))
        int(user["id"])
    except Exception:
        raise ValueError("bad init data")
    if time.time() - auth_date > max_age:
        raise ValueError("expired")
    out = dict(data_with_sig)
    out["user"] = user
    return out


# ---- Per-group private link token (goes in ?startapp=) -----------------------------------
def make_group_token(chat_id: int, version: int) -> str:
    cid = str(chat_id).replace("-", "m")
    return f"{cid}x{version}x{_hmac(f'grp:{chat_id}:{version}', 20)}"


def parse_group_token(token: str) -> tuple[int, int]:
    try:
        cid, ver, sig = token.split("x")
        chat_id = int(cid.replace("m", "-"))
        version = int(ver)
    except Exception:
        raise ValueError("bad link")
    if not hmac.compare_digest(sig, _hmac(f"grp:{chat_id}:{version}", 20)):
        raise ValueError("bad link")
    return chat_id, version


# ---- Session token (API + WebSocket + media URLs) -----------------------------------------
def make_session(uid: int, chat_id: int, ttl: int = 12 * 3600) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"u": uid, "c": chat_id, "e": int(time.time()) + ttl}).encode()).decode().rstrip("=")
    return f"{payload}.{_hmac('s:' + payload, 40)}"


def read_session(token: str) -> tuple[int, int]:
    try:
        payload, sig = token.split(".")
    except Exception:
        raise ValueError("bad session")
    if not hmac.compare_digest(sig, _hmac("s:" + payload, 40)):
        raise ValueError("bad session")
    try:
        d = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp, uid, chat_id = float(d["e"]), int(d["u"]), int(d["c"])
    except Exception:
        raise ValueError("bad session")
    if exp < time.time():
        raise ValueError("session expired")
    return uid, chat_id
