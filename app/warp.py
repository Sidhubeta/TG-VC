"""Cloudflare WARP as a proxy for everything the server asks YouTube for.

YouTube treats datacenter addresses (Railway's) as bots: "Sign in to confirm you're not a bot", 429 Too Many
Requests, 403 on song files. Settings in yt-dlp can't change the address YouTube sees. WARP can: wgcf registers a
free WARP account (once, kept on the volume) and wireproxy runs it as a local HTTP proxy, in user space (no root, no
VPN device). Lookups (yt-dlp), song downloads and ffmpeg all go through it: YouTube's song links only work from the
address that looked the song up.

Turned on with YT_WARP=1. If anything here fails, the server simply goes to YouTube directly as before, and the
Health page says why. YT_PROXY (any proxy, e.g. a paid residential one) takes precedence over WARP.
"""
import asyncio
import logging
import os
import shutil
import time

from . import config
from .http import httpx

log = logging.getLogger("warp")
PORT = 25345
DIR = os.path.join(config.DATA_DIR, "warp")
STATE: dict = {"ok": None, "detail": "Off (set YT_WARP=1 to send YouTube requests through Cloudflare WARP)",
               "ip": "", "checked": 0.0}
_proc: asyncio.subprocess.Process | None = None


def _bin(name: str) -> str | None:
    return shutil.which(name) or (f"/usr/local/bin/{name}" if os.path.exists(f"/usr/local/bin/{name}") else None)


def proxy_url() -> str | None:
    """The proxy to use for YouTube right now, or None (straight to YouTube)."""
    if config.YT_PROXY:
        return config.YT_PROXY
    if _proc is not None and _proc.returncode is None and STATE["ok"]:
        return f"http://127.0.0.1:{PORT}"
    return None


async def _run(args: list[str], timeout: float = 60) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*args, cwd=DIR, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 1, "timed out"
    return p.returncode or 0, out.decode(errors="replace")[-300:]


def _conf_text() -> str:
    return (f"WGConfig = {os.path.join(DIR, 'wgcf-profile.conf')}\n\n"
            f"[http]\nBindAddress = 127.0.0.1:{PORT}\n")


async def _check() -> bool:
    """Is the proxy up, and does traffic leave through WARP? (Cloudflare's trace page says warp=on.)"""
    try:
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{PORT}", timeout=10) as c:
            r = await c.get("https://www.cloudflare.com/cdn-cgi/trace")
        info = dict(line.split("=", 1) for line in r.text.splitlines() if "=" in line)
        STATE["ip"] = info.get("ip", "")
        return info.get("warp") in ("on", "plus")
    except Exception as e:
        STATE["detail"] = f"WARP proxy not answering: {str(e) or type(e).__name__}"[:200]
        return False


async def start():
    """Set up and keep WARP running (a loop: restarts the proxy if it stops)."""
    global _proc
    if config.YT_PROXY:
        STATE.update(ok=True, detail="Using YT_PROXY for YouTube")
        return
    if not config.YT_WARP:
        return
    wgcf, wireproxy = _bin("wgcf"), _bin("wireproxy")
    if not (wgcf and wireproxy):
        STATE.update(ok=False, detail="WARP tools missing in this build (wgcf / wireproxy): going to YouTube directly")
        log.warning(STATE["detail"])
        return
    os.makedirs(DIR, exist_ok=True)
    fails = 0
    while True:
        try:
            if not os.path.exists(os.path.join(DIR, "wgcf-account.toml")):
                code, out = await _run([wgcf, "register", "--accept-tos"])
                if code:
                    raise RuntimeError(f"WARP sign-up failed: {out}")
            if not os.path.exists(os.path.join(DIR, "wgcf-profile.conf")):
                code, out = await _run([wgcf, "generate"])
                if code:
                    raise RuntimeError(f"WARP profile failed: {out}")
            conf = os.path.join(DIR, "wireproxy.conf")
            with open(conf, "w") as fh:
                fh.write(_conf_text())
            _proc = await asyncio.create_subprocess_exec(wireproxy, "-c", conf, cwd=DIR,
                                                         stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.DEVNULL)
            for _ in range(20):  # (the tunnel takes a moment)
                await asyncio.sleep(1)
                if _proc.returncode is not None or await _check():
                    break
            if _proc.returncode is None and await _check():
                STATE.update(ok=True, detail=f"On: YouTube sees Cloudflare WARP ({STATE['ip']})", checked=time.time())
                log.info("WARP on: %s", STATE["ip"])
                fails = 0
                while _proc.returncode is None:  # keep an eye on it
                    await asyncio.sleep(60)
                    if not await _check():
                        STATE.update(ok=False)
                        break
                    STATE["checked"] = time.time()
            if _proc.returncode is None:
                _proc.kill()
            raise RuntimeError(STATE["detail"] if STATE["ok"] is False else "WARP proxy stopped")
        except asyncio.CancelledError:
            if _proc is not None and _proc.returncode is None:
                _proc.kill()
            raise
        except Exception as e:
            fails += 1
            STATE.update(ok=False, detail=f"{str(e)[:180]} — going to YouTube directly meanwhile")
            log.warning("WARP: %s", e)
            if fails >= 3 and "sign-up" not in str(e):
                # a profile that never works (Cloudflare changed something): make a fresh one next time
                for f in ("wgcf-profile.conf",):
                    try:
                        os.remove(os.path.join(DIR, f))
                    except OSError:
                        pass
            await asyncio.sleep(min(600, 15 * fails))


def status() -> dict:
    if config.YT_PROXY:
        return {"ok": True, "detail": "YT_PROXY is set: YouTube requests go through it"}
    return {"ok": STATE["ok"], "detail": STATE["detail"]}
