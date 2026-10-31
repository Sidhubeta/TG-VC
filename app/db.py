"""SQLite storage (stdlib, run in a thread). Put DATA_DIR on a Railway volume to keep data."""
import asyncio
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config

_PATH = os.path.join(config.DATA_DIR, "vc.db")
_lock = threading.Lock()
_conn = sqlite3.connect(_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups(
  chat_id INTEGER PRIMARY KEY, title TEXT, link_version INTEGER DEFAULT 1,
  settings TEXT DEFAULT '{}', updated REAL);
CREATE TABLE IF NOT EXISTS users(
  uid INTEGER PRIMARY KEY, name TEXT, username TEXT, photo TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, uid INTEGER, name TEXT, username TEXT,
  joined REAL, left REAL, last_seen REAL, mic_secs REAL DEFAULT 0, open INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_sess_chat ON sessions(chat_id, joined);
CREATE INDEX IF NOT EXISTS ix_sess_uid ON sessions(chat_id, uid);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, uid INTEGER, name TEXT, text TEXT,
  reply TEXT, reactions TEXT DEFAULT '{}', ts REAL);
CREATE INDEX IF NOT EXISTS ix_msg_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS plays(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, vid TEXT, title TEXT, artist TEXT,
  duration REAL, by_uid INTEGER, by_name TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS ix_plays_chat ON plays(chat_id, ts);
CREATE TABLE IF NOT EXISTS playlists(
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, name TEXT, created REAL);
CREATE INDEX IF NOT EXISTS ix_pl_uid ON playlists(uid);
CREATE TABLE IF NOT EXISTS playlist_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER, track TEXT, added REAL);
CREATE INDEX IF NOT EXISTS ix_pli_pl ON playlist_items(playlist_id, id);
CREATE TABLE IF NOT EXISTS bans(
  chat_id INTEGER, uid INTEGER, name TEXT, by_uid INTEGER, ts REAL, PRIMARY KEY(chat_id, uid));
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, uid INTEGER, name TEXT,
  by_uid INTEGER, by_name TEXT, reason TEXT, ts REAL, resolved INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_rep_chat ON reports(chat_id, resolved);
CREATE TABLE IF NOT EXISTS schedules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, title TEXT, at REAL, by_uid INTEGER, sent INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_sched ON schedules(sent, at);
CREATE TABLE IF NOT EXISTS listens(
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, vid TEXT, title TEXT, artist TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS ix_listens_uid ON listens(uid, ts);
CREATE TABLE IF NOT EXISTS sudo(uid INTEGER PRIMARY KEY, name TEXT, username TEXT, added_by INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS room_state(chat_id INTEGER PRIMARY KEY, data TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS members(
  chat_id INTEGER, uid INTEGER, name TEXT, username TEXT, seen REAL, PRIMARY KEY(chat_id, uid));
"""
_conn.executescript(SCHEMA)
for _sql in ("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''",
             "ALTER TABLE groups ADD COLUMN pin_msg INTEGER",
             "ALTER TABLE groups ADD COLUMN pin_thread INTEGER",
             # sent / played "as the group": kept, so it stays hidden after a restart too
             "ALTER TABLE messages ADD COLUMN asg INTEGER DEFAULT 0",
             "ALTER TABLE plays ADD COLUMN asg INTEGER DEFAULT 0"):
    try:
        _conn.execute(_sql)
    except sqlite3.OperationalError:
        pass


def q_sync(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


# The database gets its own threads: in the shared pool, YouTube lookups and ffmpeg (seconds each) could fill
# every thread during a Spotify import, and logins, chat and every other query waited behind them.
_POOL = ThreadPoolExecutor(2, thread_name_prefix="db")


def _run(fn, *args):
    return asyncio.get_running_loop().run_in_executor(_POOL, fn, *args)


async def q(sql, args=()):
    return await _run(q_sync, sql, args)


async def q1(sql, args=()):
    rows = await q(sql, args)
    return rows[0] if rows else None


def x_sync(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.lastrowid


async def x(sql, args=()):
    return await _run(x_sync, sql, args)


def xmany_sync(sql, rows):
    with _lock:
        _conn.execute("BEGIN")
        try:
            _conn.executemany(sql, rows)
            _conn.execute("COMMIT")
        except Exception:
            _conn.execute("ROLLBACK")
            raise


async def xmany(sql, rows):
    """Many writes in one transaction and one thread hop (much faster than a loop of x())."""
    rows = list(rows)
    if rows:
        await _run(xmany_sync, sql, rows)


# --- helpers -------------------------------------------------------------
async def get_group(chat_id: int) -> dict:
    g = await q1("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
    if not g:
        await x("INSERT OR IGNORE INTO groups(chat_id,title,updated) VALUES(?,?,?)", (chat_id, "", time.time()))
        g = await q1("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
    g["settings"] = json.loads(g.get("settings") or "{}")
    return g


async def save_group_settings(chat_id: int, settings: dict):
    await x("UPDATE groups SET settings=?, updated=? WHERE chat_id=?", (json.dumps(settings), time.time(), chat_id))


def close_stale_sessions():
    """After a crash/restart, close sessions that were left open."""
    x_sync("UPDATE sessions SET open=0, left=COALESCE(last_seen, joined) WHERE open=1")


close_stale_sessions()
