#!/usr/bin/env python3
"""
PingMon v3 — Asynchronous Network Device Monitor
Backend : asyncio ping sweep + SQLite
Frontend: Flask + Waitress (cross-platform, multi-threaded)
Auth    : session-based login, User / Administrator roles
"""

import asyncio
import configparser
import csv
import io
import logging
import os
import random
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request, session, url_for
from waitress import serve
from werkzeug.security import check_password_hash, generate_password_hash

# ── Logging ──────────────────────────────────────────────────────────────────

def _enable_windows_ansi():
    """Enable ANSI escape codes in Windows Terminal / conhost if needed."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # Get current console mode and OR in ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode   = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass  # Non-fatal: colours just won't render in very old hosts

_enable_windows_ansi()


class CustomFormatter(logging.Formatter):
    """Colourised log formatter — works on Windows Terminal, macOS, Linux."""

    _grey     = "\x1b[38;20m"
    _cyan     = "\x1b[36;20m"
    _yellow   = "\x1b[33;20m"
    _red      = "\x1b[31;20m"
    _bold_red = "\x1b[31;1m"
    _reset    = "\x1b[0m"

    _fmt = "%(asctime)s [%(levelname)s] %(message)s"

    FORMATS = {
        logging.DEBUG:    _grey     + _fmt + _reset,
        logging.INFO:     _cyan     + _fmt + _reset,
        logging.WARNING:  _yellow   + _fmt + _reset,
        logging.ERROR:    _red      + _fmt + _reset,
        logging.CRITICAL: _bold_red + _fmt + _reset,
    }

    def format(self, record):
        log_fmt   = self.FORMATS.get(record.levelno, self._fmt)
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def _build_logger() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CustomFormatter())
    logger = logging.getLogger("pingmon")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False   # don't double-print via the root logger
    return logger

log = _build_logger()

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent.resolve()
CONFIG_FILE     = BASE_DIR / "pingmon.conf"
DB_FILE         = BASE_DIR / "pingmon.db"
SECRET_KEY_FILE = BASE_DIR / "pingmon.secret"

# ── Shared state ──────────────────────────────────────────────────────────────
cfg            = {}
last_sweep_time = None
next_sweep_time = None
state_lock      = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

def _load_or_create_secret_key() -> str:
    """
    Session cookies are signed with this key. Kept in its own file (not
    pingmon.conf) so we never have to rewrite — and risk stripping comments
    from — the user's configuration file. Generated once, then reused on
    every future start so logged-in sessions survive a restart.
    """
    if SECRET_KEY_FILE.exists():
        key = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_hex(32)
    try:
        SECRET_KEY_FILE.write_text(key, encoding="utf-8")
        try:
            os.chmod(SECRET_KEY_FILE, 0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX permissions
        log.info("Generated new session secret key: %s", SECRET_KEY_FILE.name)
    except OSError as e:
        log.warning("Could not persist secret key to %s (%s) — "
                     "sessions will not survive a restart.", SECRET_KEY_FILE.name, e)
    return key

def load_config():
    global cfg
    p = configparser.ConfigParser()
    p.read(CONFIG_FILE, encoding="utf-8-sig")
    cfg = {
        "port":             p.getint("server",  "port",               fallback=9090),
        "threads":          p.getint("server",  "threads",            fallback=8),
        "interval_seconds": p.getint("ping",    "interval_seconds",   fallback=300),
        "ping_timeout":     p.getint("ping",    "timeout_seconds",    fallback=2),
        "ping_count":       p.getint("ping",    "count",              fallback=1),
        "jitter_max":       p.getint("ping",    "jitter_max_seconds", fallback=8),
        "db_file":          p.get(   "database","file",               fallback=str(DB_FILE)),
        "secret_key":       _load_or_create_secret_key(),
    }
    log.info("Config → port=%s  interval=%ss  threads=%s",
             cfg["port"], cfg["interval_seconds"], cfg["threads"])

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(cfg.get("db_file", str(DB_FILE)), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent readers + one writer
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

_db_lock = threading.Lock()

def init_db():
    with get_db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS ping_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_type TEXT    NOT NULL,
                hostname    TEXT    NOT NULL,
                ip_address  TEXT,
                location    TEXT,
                status      INTEGER NOT NULL,
                latency_ms  REAL,
                pinged_at   TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pinged_at   ON ping_results(pinged_at);
            CREATE INDEX IF NOT EXISTS idx_device_type ON ping_results(device_type);
            CREATE INDEX IF NOT EXISTS idx_hostname    ON ping_results(hostname);

            CREATE TABLE IF NOT EXISTS users (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                username              TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                password_hash         TEXT    NOT NULL,
                role                  TEXT    NOT NULL CHECK(role IN ('user','admin')),
                must_change_password  INTEGER NOT NULL DEFAULT 0,
                created_at            TEXT    NOT NULL,
                last_login            TEXT
            );
        """)
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c.execute(
                """INSERT INTO users (username, password_hash, role, must_change_password, created_at)
                   VALUES (?,?,?,?,?)""",
                ("admin", generate_password_hash("admin"), "admin", 1, now),
            )
            log.warning("No user accounts found — created default account 'admin' / 'admin'. "
                        "This account must set a new password at first login.")
    log.info("Database ready: %s", cfg.get("db_file", DB_FILE))

def save_result(device_type, hostname, ip_address, location, status, latency_ms):
    with _db_lock:
        with get_db() as c:
            c.execute(
                """INSERT INTO ping_results
                   (device_type,hostname,ip_address,location,status,latency_ms,pinged_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (device_type, hostname, ip_address, location,
                 1 if status else 0, latency_ms,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )

def query_latest_per_host(device_type=None):
    with get_db() as c:
        if device_type:
            rows = c.execute("""
                SELECT p.* FROM ping_results p
                JOIN (SELECT hostname,device_type,MAX(pinged_at) ma
                      FROM ping_results WHERE device_type=?
                      GROUP BY hostname,device_type) m
                  ON p.hostname=m.hostname AND p.device_type=m.device_type AND p.pinged_at=m.ma
                ORDER BY p.hostname
            """, (device_type,)).fetchall()
        else:
            rows = c.execute("""
                SELECT p.* FROM ping_results p
                JOIN (SELECT hostname,device_type,MAX(pinged_at) ma
                      FROM ping_results GROUP BY hostname,device_type) m
                  ON p.hostname=m.hostname AND p.device_type=m.device_type AND p.pinged_at=m.ma
                ORDER BY p.device_type,p.hostname
            """).fetchall()
        return [dict(r) for r in rows]

def query_device_history(hostname, hours=24):
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as c:
        rows = c.execute("""
            SELECT pinged_at, status, latency_ms, ip_address
            FROM ping_results
            WHERE hostname=? AND pinged_at>=?
            ORDER BY pinged_at ASC
        """, (hostname, since)).fetchall()
        return [dict(r) for r in rows]

def query_dashboard_stats():
    latest = _filter_to_configured(query_latest_per_host())
    total  = len(latest)
    up     = sum(1 for r in latest if r["status"] == 1)
    by_type = {}
    for r in latest:
        dt = r["device_type"]
        by_type.setdefault(dt, {"up": 0, "down": 0})
        if r["status"] == 1:
            by_type[dt]["up"] += 1
        else:
            by_type[dt]["down"] += 1
    return {"total": total, "up": up, "down": total - up, "by_type": by_type}

def get_device_types():
    with get_db() as c:
        rows = c.execute(
            "SELECT DISTINCT device_type FROM ping_results ORDER BY device_type"
        ).fetchall()
        return [r["device_type"] for r in rows]

# ═══════════════════════════════════════════════════════════════════════════════
# USER ACCOUNTS
# ═══════════════════════════════════════════════════════════════════════════════

def get_user_by_username(username):
    with get_db() as c:
        row = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        return dict(row) if row else None

def get_user_by_id(user_id):
    with get_db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

def list_users():
    with get_db() as c:
        rows = c.execute("""
            SELECT id, username, role, must_change_password, created_at, last_login
            FROM users ORDER BY username COLLATE NOCASE
        """).fetchall()
        return [dict(r) for r in rows]

def count_admins():
    with get_db() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()
        return row["n"]

def create_user(username, password, role, must_change=True):
    username = (username or "").strip()
    password = password or ""
    if not username:
        raise ValueError("Username is required.")
    if not password:
        raise ValueError("Password is required.")
    if role not in ("user", "admin"):
        raise ValueError("Invalid role.")
    with _db_lock:
        with get_db() as c:
            existing = c.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
            if existing:
                raise ValueError(f'A user named "{username}" already exists.')
            try:
                c.execute(
                    """INSERT INTO users (username,password_hash,role,must_change_password,created_at)
                       VALUES (?,?,?,?,?)""",
                    (username, generate_password_hash(password), role,
                     1 if must_change else 0,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f'A user named "{username}" already exists.')

def set_user_role(user_id, role):
    if role not in ("user", "admin"):
        raise ValueError("Invalid role.")
    with _db_lock:
        with get_db() as c:
            c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))

def reset_user_password(user_id, new_password, force_change=True):
    with _db_lock:
        with get_db() as c:
            c.execute(
                "UPDATE users SET password_hash=?, must_change_password=? WHERE id=?",
                (generate_password_hash(new_password), 1 if force_change else 0, user_id),
            )

def delete_user(user_id):
    with _db_lock:
        with get_db() as c:
            c.execute("DELETE FROM users WHERE id=?", (user_id,))

def touch_last_login(user_id):
    with _db_lock:
        with get_db() as c:
            c.execute("UPDATE users SET last_login=? WHERE id=?",
                      (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))

# ═══════════════════════════════════════════════════════════════════════════════
# INPUT FILE PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_dat_file(path: Path):
    devices = []
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("!"):
                    continue
                parts = line.split(",", 1)
                hostname = parts[0].strip()
                location = parts[1].strip() if len(parts) > 1 else ""
                if hostname:
                    devices.append((hostname, location))
    except OSError as e:
        log.warning("Cannot read %s: %s", path, e)
    return devices

def discover_dat_files(log_summary=False):
    """
    Read every .DAT file in BASE_DIR.

    log_summary defaults to False because this function is called from two
    very different places: once per sweep cycle from sweep_loop() (where a
    log line per group is useful), and once per web request from
    get_configured_device_keys() (where it would spam the log on every page
    view). Only the sweep passes log_summary=True.
    """
    result = {}
    seen = set()
    patterns = list(BASE_DIR.glob("*.dat")) + list(BASE_DIR.glob("*.DAT"))
    for dat in sorted(set(patterns)):
        key = dat.stem.upper()
        if key in seen:
            continue
        seen.add(key)
        devices = parse_dat_file(dat)
        if devices:
            result[key] = devices
            if log_summary:
                log.info("Loaded %d devices from %s", len(devices), dat.name)
    return result

def get_configured_device_keys():
    """(DEVICE_TYPE, hostname-lowercased) pairs currently listed in the .DAT files on disk."""
    keys = set()
    for dt, devices in discover_dat_files().items():
        for hostname, _location in devices:
            keys.add((dt, hostname.strip().lower()))
    return keys

def _filter_to_configured(rows):
    """
    Drop rows for devices that have been removed from their .DAT file.
    ping_results keeps every historical row forever, so without this filter a
    deleted device would keep showing up (frozen at its last known status)
    indefinitely — no refresh would ever make it disappear, since the table was
    built purely from history, not from the current device list. Ping history
    for removed devices is left intact in the database; it just stops being
    surfaced in the dashboard / group tables.
    """
    configured = get_configured_device_keys()
    return [r for r in rows if (r["device_type"], r["hostname"].strip().lower()) in configured]

# ═══════════════════════════════════════════════════════════════════════════════
# DEVICE GROUP FILE WRITES  (add / edit / delete devices, create new groups)
# ═══════════════════════════════════════════════════════════════════════════════

_file_lock = threading.Lock()

VALID_GROUP_NAME = re.compile(r'^[A-Za-z0-9_\-]+$')
VALID_HOSTNAME   = re.compile(r'^[A-Za-z0-9._\-]+$')

def group_file_path(device_type):
    key = (device_type or "").strip().upper()
    for ext in ("*.dat", "*.DAT"):
        for f in BASE_DIR.glob(ext):
            if f.stem.upper() == key:
                return f
    return None

def _read_dat_lines(path):
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        return fh.read().splitlines()

def _write_dat_lines(path, lines):
    """Atomic write: build the new content in a temp file, then swap it in."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")
    os.replace(tmp, path)

def _find_device_line_index(lines, hostname):
    target = hostname.strip().lower()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("!"):
            continue
        host = s.split(",", 1)[0].strip().lower()
        if host == target:
            return i
    return -1

def add_device_to_group(device_type, hostname, location):
    hostname = (hostname or "").strip()
    location = (location or "").strip()
    if not hostname:
        raise ValueError("Hostname / IP is required.")
    if not VALID_HOSTNAME.match(hostname):
        raise ValueError("Hostname / IP contains invalid characters.")
    path = group_file_path(device_type)
    if not path:
        raise ValueError(f'Device group "{device_type}" was not found.')
    with _file_lock:
        lines = _read_dat_lines(path)
        if _find_device_line_index(lines, hostname) != -1:
            raise ValueError(f'"{hostname}" already exists in this group.')
        lines.append(f"{hostname}, {location}" if location else hostname)
        _write_dat_lines(path, lines)
    log.info("[%s] device added: %s (%s)", device_type.upper(), hostname, location)

def edit_device_in_group(device_type, old_hostname, new_hostname, new_location):
    new_hostname = (new_hostname or "").strip()
    new_location = (new_location or "").strip()
    if not new_hostname:
        raise ValueError("Hostname / IP is required.")
    if not VALID_HOSTNAME.match(new_hostname):
        raise ValueError("Hostname / IP contains invalid characters.")
    path = group_file_path(device_type)
    if not path:
        raise ValueError(f'Device group "{device_type}" was not found.')
    with _file_lock:
        lines = _read_dat_lines(path)
        idx = _find_device_line_index(lines, old_hostname)
        if idx == -1:
            raise ValueError(f'"{old_hostname}" was not found in this group.')
        if new_hostname.lower() != old_hostname.strip().lower():
            dupe = _find_device_line_index(lines, new_hostname)
            if dupe != -1 and dupe != idx:
                raise ValueError(f'"{new_hostname}" already exists in this group.')
        lines[idx] = f"{new_hostname}, {new_location}" if new_location else new_hostname
        _write_dat_lines(path, lines)
    log.info("[%s] device edited: %s -> %s (%s)",
             device_type.upper(), old_hostname, new_hostname, new_location)

def delete_device_from_group(device_type, hostname):
    path = group_file_path(device_type)
    if not path:
        raise ValueError(f'Device group "{device_type}" was not found.')
    with _file_lock:
        lines = _read_dat_lines(path)
        idx = _find_device_line_index(lines, hostname)
        if idx == -1:
            raise ValueError(f'"{hostname}" was not found in this group.')
        del lines[idx]
        _write_dat_lines(path, lines)
    log.info("[%s] device removed: %s", device_type.upper(), hostname)

def create_group_file(name):
    key = (name or "").strip().upper()
    if not key:
        raise ValueError("Group name is required.")
    if not VALID_GROUP_NAME.match(key):
        raise ValueError("Group name may only contain letters, numbers, underscores and hyphens.")
    if group_file_path(key):
        raise ValueError(f'A device group named "{key}" already exists.')
    path = BASE_DIR / f"{key}.DAT"
    with _file_lock:
        path.write_text(
            f"! {key}\n!\n! Format:  Hostname-or-IP, Physical Location Description\n!\n",
            encoding="utf-8",
        )
    log.info("New device group created: %s", path.name)
    return path

# ═══════════════════════════════════════════════════════════════════════════════
# PING
# ═══════════════════════════════════════════════════════════════════════════════

async def async_ping(hostname):
    timeout = cfg.get("ping_timeout", 2)
    count   = cfg.get("ping_count",   1)
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", str(count), "-w", str(timeout * 1000), hostname]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(timeout), hostname]
    try:
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
        elapsed_ms = (time.monotonic() - t0) * 1000
        if proc.returncode == 0:
            out = stdout.decode(errors="replace")
            m = re.search(r"time[=<]([\d.]+)\s*ms", out, re.IGNORECASE)
            latency = float(m.group(1)) if m else round(elapsed_ms, 1)
            return True, round(latency, 1)
        return False, None
    except (asyncio.TimeoutError, OSError):
        return False, None

async def ping_device(device_type, hostname, location):
    await asyncio.sleep(random.uniform(0, cfg.get("jitter_max", 8)))
    try:
        ip_address = socket.gethostbyname(hostname)
    except Exception:
        ip_address = hostname
    success, latency = await async_ping(hostname)
    log.info("[%s] %-30s %-15s %s %s",
             device_type, hostname, ip_address,
             "UP" if success else "DOWN",
             f"{latency}ms" if latency else "")
    save_result(device_type, hostname, ip_address, location, success, latency)

async def run_sweep(devices_by_type):
    global last_sweep_time, next_sweep_time
    with state_lock:
        last_sweep_time = datetime.now()
        next_sweep_time = last_sweep_time + timedelta(seconds=cfg.get("interval_seconds", 300))
    log.info("═══ Sweep start — %d groups ═══", len(devices_by_type))
    tasks = [ping_device(dt, h, loc)
             for dt, devs in devices_by_type.items()
             for h, loc in devs]
    await asyncio.gather(*tasks)
    log.info("═══ Sweep complete ═══")

def sweep_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        devices = discover_dat_files(log_summary=True)
        if devices:
            loop.run_until_complete(run_sweep(devices))
        else:
            log.warning("No device .DAT files found in %s", BASE_DIR)
        time.sleep(cfg.get("interval_seconds", 300))

# ═══════════════════════════════════════════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

# ── Auth helpers ────────────────────────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_user_by_id(uid)

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        user = current_user()  # fresh lookup — don't trust a possibly-stale session role
        if not user or user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped

# ── Shared CSS + layout ───────────────────────────────────────────────────────
BASE_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>

<!-- ── Theme: apply before first paint to prevent flash ── -->
<script>
(function(){
  const t = localStorage.getItem('pingmon-theme') || 'night';
  document.documentElement.setAttribute('data-theme', t);
})();
</script>

<style>
/* ── Night theme (default) ── */
:root, [data-theme="night"] {
  --bg:      #0d1117;
  --surface: #161b22;
  --border:  #30363d;
  --accent:  #00e5ff;
  --up:      #22c55e;
  --down:    #ef4444;
  --warn:    #f59e0b;
  --text:    #e6edf3;
  --muted:   #8b949e;
  --shadow:  rgba(0,0,0,.4);
  --row-hover: rgba(0,229,255,.04);
  --bar-empty: rgba(255,255,255,.07);
  --badge-up-bg:   rgba(34,197,94,.15);
  --badge-down-bg: rgba(239,68,68,.15);
  --chart-grid:    rgba(255,255,255,.06);
  --chart-border:  rgba(255,255,255,.1);
  --chart-tick:    rgba(255,255,255,.4);
  --theme-btn-icon: "☀️";
}

/* ── Day theme ── */
[data-theme="day"] {
  --bg:      #f0f4f8;
  --surface: #ffffff;
  --border:  #d0d7de;
  --accent:  #0969da;
  --up:      #1a7f37;
  --down:    #cf222e;
  --warn:    #9a6700;
  --text:    #1f2328;
  --muted:   #57606a;
  --shadow:  rgba(0,0,0,.1);
  --row-hover: rgba(9,105,218,.04);
  --bar-empty: rgba(0,0,0,.07);
  --badge-up-bg:   rgba(26,127,55,.12);
  --badge-down-bg: rgba(207,34,46,.12);
  --chart-grid:    rgba(0,0,0,.06);
  --chart-border:  rgba(0,0,0,.12);
  --chart-tick:    rgba(0,0,0,.45);
  --theme-btn-icon: "🌙";
}

/* ── Base ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:15px}
body{
  background:var(--bg);color:var(--text);
  font-family:'Syne',sans-serif;min-height:100vh;
  transition:background .25s,color .25s;
}

/* ── Header ── */
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:.9rem 2rem;border-bottom:1px solid var(--border);
  background:var(--surface);position:sticky;top:0;z-index:100;
  box-shadow:0 1px 4px var(--shadow);
}
.header-left{display:flex;align-items:center;gap:.75rem}
.logo{font-size:1.35rem;font-weight:800;letter-spacing:-.03em;color:var(--accent);text-transform:uppercase}
.logo span{color:var(--text)}
.tagline{font-size:.72rem;color:var(--muted);font-family:'JetBrains Mono',monospace}

/* ── Header (auth) ── */
.header-right{display:flex;align-items:center;gap:.85rem}
.user-chip{display:flex;align-items:center;gap:.55rem;font-size:.75rem;font-family:'JetBrains Mono',monospace;color:var(--muted)}
.user-name{color:var(--text);font-weight:600}
.role-badge{padding:.15rem .55rem;border-radius:99px;font-size:.62rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase}
.role-badge.admin{background:rgba(0,229,255,.15);color:var(--accent)}
.role-badge.user{background:var(--bar-empty);color:var(--muted)}
.alert-inline{font-size:.63rem;color:var(--warn);font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:.04em}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:.35rem;padding:.4rem 1rem;border-radius:6px;
  border:1px solid var(--border);background:transparent;color:var(--text);cursor:pointer;
  font-size:.75rem;font-weight:700;letter-spacing:.03em;font-family:'Syne',sans-serif;
  text-decoration:none;transition:all .15s;white-space:nowrap}
.btn:hover{border-color:var(--accent);color:var(--accent);background:var(--row-hover)}
.btn-sm{padding:.3rem .75rem;font-size:.68rem}
.btn-primary{background:var(--accent);border-color:var(--accent);color:#04222b}
.btn-primary:hover{filter:brightness(1.08);color:#04222b;background:var(--accent)}
.btn-danger{border-color:var(--down);color:var(--down)}
.btn-danger:hover{background:var(--badge-down-bg);color:var(--down);border-color:var(--down)}
.btn-ghost{border-color:transparent;color:var(--muted)}
.btn-ghost:hover{color:var(--accent);border-color:var(--border);background:var(--row-hover)}
.inline-form{display:inline-block;margin-left:.4rem}
td.actions{white-space:nowrap}

/* ── Forms ── */
.auth-wrap{display:flex;justify-content:center;padding-top:2.5rem}
.form-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:1.6rem 1.75rem;box-shadow:0 1px 3px var(--shadow);max-width:440px;width:100%}
.form-group{margin-bottom:1.1rem}
.form-label{display:block;font-size:.68rem;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);margin-bottom:.4rem}
.form-input,.form-select{width:100%;padding:.55rem .75rem;background:var(--bg);
  border:1px solid var(--border);border-radius:6px;color:var(--text);
  font-family:'JetBrains Mono',monospace;font-size:.85rem;outline:none;transition:border-color .15s}
.form-input:focus,.form-select:focus{border-color:var(--accent)}
.form-actions{display:flex;gap:.6rem;margin-top:1.4rem;flex-wrap:wrap}
.form-hint{font-size:.7rem;color:var(--muted);margin-top:.4rem}

/* ── Alerts ── */
.alert{padding:.75rem 1rem;border-radius:8px;font-size:.8rem;margin-bottom:1.25rem;
  border:1px solid transparent;line-height:1.4}
.alert-error{background:var(--badge-down-bg);color:var(--down);border-color:var(--down)}
.alert-success{background:var(--badge-up-bg);color:var(--up);border-color:var(--up)}
.alert-warn{background:rgba(245,158,11,.12);color:var(--warn);border-color:var(--warn)}

/* ── Theme toggle button ── */
#themeBtn{
  display:flex;align-items:center;gap:.5rem;
  padding:.35rem .9rem;border-radius:20px;
  border:1px solid var(--border);background:transparent;
  color:var(--muted);cursor:pointer;font-size:.75rem;font-weight:600;
  font-family:'Syne',sans-serif;letter-spacing:.04em;text-transform:uppercase;
  transition:border-color .15s,color .15s,background .15s;
  white-space:nowrap;
}
#themeBtn:hover{border-color:var(--accent);color:var(--accent);background:var(--row-hover)}
#themeBtn .icon{font-size:.9rem;line-height:1}

/* ── Tab bar ── */
.tab-bar{
  display:flex;gap:.2rem;padding:.7rem 2rem 0;
  border-bottom:1px solid var(--border);background:var(--surface);overflow-x:auto;
}
.tab-bar a{
  display:block;padding:.45rem 1.1rem;font-size:.75rem;font-weight:700;
  letter-spacing:.06em;text-transform:uppercase;text-decoration:none;color:var(--muted);
  border-radius:6px 6px 0 0;border:1px solid transparent;border-bottom:none;
  transition:color .15s,background .15s;white-space:nowrap;
}
.tab-bar a:hover{color:var(--text);background:var(--row-hover)}
.tab-bar a.active{
  color:var(--accent);background:var(--bg);
  border-color:var(--border);border-bottom-color:var(--bg);margin-bottom:-1px;
}

/* ── Main ── */
main{padding:1.75rem 2rem;max-width:1440px;margin:0 auto}

/* ── Stat cards ── */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1rem;margin-bottom:1.75rem}
.stat-card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:1.1rem 1.4rem;
  box-shadow:0 1px 3px var(--shadow);
}
.stat-card .label{font-size:.68rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:.45rem}
.stat-card .value{font-size:1.9rem;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1}
.stat-card .value.up{color:var(--up)}
.stat-card .value.down{color:var(--down)}
.stat-card .value.accent{color:var(--accent)}
.stat-card .sub{font-size:.7rem;color:var(--muted);margin-top:.35rem;font-family:'JetBrains Mono',monospace}

/* ── Health bars ── */
.section-title{font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:.9rem}
.health-bars{display:flex;flex-direction:column;gap:.55rem;margin-bottom:1.75rem}
.health-row{display:grid;grid-template-columns:170px 1fr 55px;align-items:center;gap:1rem}
.health-row .name{font-size:.78rem;font-family:'JetBrains Mono',monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-track{height:11px;background:var(--bar-empty);border-radius:99px;overflow:hidden}
.bar-fill{height:100%;border-radius:99px;transition:width .6s ease}
.bar-fill.full{background:var(--up)}
.bar-fill.warn{background:var(--warn)}
.bar-fill.danger{background:var(--down)}
.health-row .pct{font-size:.78rem;font-family:'JetBrains Mono',monospace;color:var(--muted);text-align:right}

/* ── Table ── */
.table-wrapper{
  background:var(--surface);border:1px solid var(--border);
  border-radius:10px;overflow:auto;box-shadow:0 1px 3px var(--shadow);
}
.table-toolbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:.85rem 1.2rem;border-bottom:1px solid var(--border);gap:.75rem;flex-wrap:wrap;
}
.toolbar-left{display:flex;align-items:center;gap:.75rem}
.toolbar-title{font-size:.82rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
.search-box{
  padding:.32rem .75rem;background:var(--bg);border:1px solid var(--border);
  border-radius:6px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.78rem;
  width:210px;outline:none;transition:border-color .15s;
}
.search-box:focus{border-color:var(--accent)}

table{width:100%;border-collapse:collapse;font-size:.8rem}
thead tr{border-bottom:1px solid var(--border)}
th{
  padding:.65rem 1rem;text-align:left;font-size:.68rem;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);cursor:pointer;white-space:nowrap;user-select:none;
}
th:hover{color:var(--text)}
th.sorted-asc .si,th.sorted-desc .si{color:var(--accent)}
.si{margin-left:.25rem;opacity:.45;font-style:normal}
tbody tr{border-bottom:1px solid var(--border);transition:background .1s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:var(--row-hover)}
td{padding:.6rem 1rem;font-family:'JetBrains Mono',monospace;vertical-align:middle}
td.hostname a{color:var(--text);text-decoration:none;font-weight:600}
td.hostname a:hover{color:var(--accent)}
td.ip a{color:var(--accent);text-decoration:none;font-size:.78rem}
td.ip a:hover{text-decoration:underline}
td.location{color:var(--muted);font-size:.76rem}
td.latency,td.timestamp{color:var(--muted);font-size:.76rem}

/* ── Badges ── */
.badge{
  display:inline-flex;align-items:center;gap:.3rem;padding:.18rem .55rem;
  border-radius:99px;font-size:.68rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
}
.badge::before{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor}
.badge.up  {background:var(--badge-up-bg);  color:var(--up)}
.badge.down{background:var(--badge-down-bg);color:var(--down)}

/* ── Misc ── */
.empty-state{padding:3rem;text-align:center;color:var(--muted);font-family:'JetBrains Mono',monospace;font-size:.83rem}
footer{
  margin-top:2.5rem;padding:.9rem 2rem;border-top:1px solid var(--border);
  font-size:.7rem;color:var(--muted);font-family:'JetBrains Mono',monospace;text-align:center;
}

/* ── History page ── */
.back-link{
  display:inline-flex;align-items:center;gap:.4rem;color:var(--muted);
  text-decoration:none;font-size:.78rem;font-family:'JetBrains Mono',monospace;margin-bottom:1.25rem;
}
.back-link:hover{color:var(--accent)}
.chart-card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:1.25rem 1.5rem;margin-bottom:1.5rem;
  box-shadow:0 1px 3px var(--shadow);
}
.chart-card h3{font-size:.72rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);margin-bottom:1rem}
.range-btns{display:flex;gap:.4rem;margin-bottom:1.25rem;flex-wrap:wrap}
.range-btn{
  padding:.3rem .9rem;border-radius:6px;border:1px solid var(--border);
  background:transparent;color:var(--muted);cursor:pointer;font-size:.75rem;font-weight:600;
  font-family:'Syne',sans-serif;letter-spacing:.05em;text-transform:uppercase;transition:all .15s;
}
.range-btn:hover{border-color:var(--accent);color:var(--text)}
.range-btn.active{background:var(--row-hover);border-color:var(--accent);color:var(--accent)}
.uptime-big{font-size:3rem;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--up);line-height:1}
.uptime-lbl{font-size:.72rem;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-top:.3rem}
</style>

<!-- ── Theme toggle logic (runs on every page) ── -->
<script>
function _pmToggleTheme(){
  const html  = document.documentElement;
  const next  = html.getAttribute('data-theme') === 'night' ? 'day' : 'night';
  html.setAttribute('data-theme', next);
  localStorage.setItem('pingmon-theme', next);
  _pmUpdateBtn();
}
function _pmUpdateBtn(){
  const btn = document.getElementById('themeBtn');
  if(!btn) return;
  const night = document.documentElement.getAttribute('data-theme') === 'night';
  btn.querySelector('.icon').textContent = night ? '☀️' : '🌙';
  btn.querySelector('.label').textContent = night ? 'Day Mode' : 'Night Mode';
}
document.addEventListener('DOMContentLoaded', _pmUpdateBtn);
</script>
"""

APP_VERSION = "v3"

def render_footer(note=""):
    """Version footer shown at the bottom of every page — dashboard, group
    tables, history, and every form (login, change password, edit device,
    new group, user management, the 403 page, etc.)."""
    suffix = f" &mdash; {note}" if note else ""
    return f"<footer>PingMon {APP_VERSION}{suffix}</footer>"

def render_header():
    user = current_user()
    if user:
        role_label = "Administrator" if user["role"] == "admin" else "User"
        role_cls   = "admin" if user["role"] == "admin" else "user"
        account_html = f"""
    <div class="user-chip">
      <span class="role-badge {role_cls}">{role_label}</span>
      <span class="user-name">{user['username']}</span>
      <a class="btn btn-ghost btn-sm" href="/account/change-password">Change Password</a>
      <a class="btn btn-ghost btn-sm" href="/logout">Log Out</a>
    </div>"""
    else:
        account_html = '<a class="btn btn-primary btn-sm" href="/login">Log In</a>'

    return f"""
<header>
  <div class="header-left">
    <div>
      <div class="logo">Ping<span>Mon</span></div>
      <div class="tagline">Network Device Monitor v3</div>
    </div>
  </div>
  <div class="header-right">
    {account_html}
    <button id="themeBtn" onclick="_pmToggleTheme()" title="Toggle Day / Night theme">
      <span class="icon">☀️</span>
      <span class="label">Day Mode</span>
    </button>
  </div>
</header>"""

def render_tabs(active):
    device_types = get_device_types()
    user = current_user()
    html = '<nav class="tab-bar">'
    html += f'<a href="/" class="{"active" if active=="dashboard" else ""}">📊 Dashboard</a>'
    for dt in device_types:
        cls = "active" if active == dt else ""
        html += f'<a href="/devices/{dt}" class="{cls}">{dt.title()}</a>'
    if user and user["role"] == "admin":
        html += f'<a href="/admin/groups/new" class="{"active" if active=="new-group" else ""}">+ New Group</a>'
        html += f'<a href="/admin/users" class="{"active" if active=="users" else ""}">👥 Users</a>'
    html += '</nav>'
    return html

TABLE_SCRIPT = """
<script>
let _sortDir={};
function sortTable(col){
  const tbl=document.getElementById('dt');
  const rows=Array.from(tbl.querySelectorAll('tbody tr'));
  const asc=!_sortDir[col]; _sortDir={}; _sortDir[col]=asc;
  rows.sort((a,b)=>{
    const av=a.cells[col].innerText.trim(), bv=b.cells[col].innerText.trim();
    return asc?av.localeCompare(bv,undefined,{numeric:true}):bv.localeCompare(av,undefined,{numeric:true});
  });
  const tb=tbl.querySelector('tbody'); rows.forEach(r=>tb.appendChild(r));
  tbl.querySelectorAll('th').forEach((th,i)=>{
    th.className=i===col?(asc?'sorted-asc':'sorted-desc'):'';
    const s=th.querySelector('.si');
    if(s) s.textContent=i===col?(asc?'↑':'↓'):'⇅';
  });
}
function filterTable(){
  const q=document.getElementById('fb').value.toLowerCase();
  document.querySelectorAll('#dt tbody tr').forEach(r=>{
    r.style.display=r.innerText.toLowerCase().includes(q)?'':'none';
  });
}
</script>
"""

@app.before_request
def _force_password_change():
    uid = session.get("user_id")
    if not uid:
        return
    if request.endpoint in ("change_password", "logout", "static"):
        return
    if session.get("must_change_password"):
        return redirect(url_for("change_password"))

@app.errorhandler(403)
def _forbidden(e):
    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — Access Denied</title>{BASE_STYLE}</head><body>
{render_header()}
<main style="max-width:600px;text-align:center;padding-top:4rem">
  <p class="section-title">403</p>
  <h2 style="font-size:1.4rem;margin-bottom:.75rem">Access Denied</h2>
  <p style="color:var(--muted)">You don't have permission to view this page. Administrator access is required.</p>
  <p style="margin-top:1.5rem"><a class="back-link" href="/" style="display:inline-flex">← Back to Dashboard</a></p>
</main>
{render_footer()}
</body></html>"""), 403

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    stats    = query_dashboard_stats()
    by_type  = stats["by_type"]
    pct_up   = round(stats["up"] / stats["total"] * 100) if stats["total"] else 0
    last_t   = last_sweep_time.strftime("%Y-%m-%d %H:%M:%S") if last_sweep_time else "—"
    next_t   = next_sweep_time.strftime("%Y-%m-%d %H:%M:%S") if next_sweep_time else "—"
    interval = cfg.get("interval_seconds", 300)

    bars_html = ""
    for dt, counts in sorted(by_type.items()):
        tot = counts["up"] + counts["down"]
        pct = round(counts["up"] / tot * 100) if tot else 0
        cls = "full" if pct == 100 else ("warn" if pct >= 50 else "danger")
        bars_html += f"""
        <div class="health-row">
          <div class="name">{dt}</div>
          <div class="bar-track"><div class="bar-fill {cls}" style="width:{pct}%"></div></div>
          <div class="pct">{pct}%</div>
        </div>"""

    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — Dashboard</title>{BASE_STYLE}</head><body>
{render_header()}{render_tabs("dashboard")}
<main>
  <div class="stat-grid">
    <div class="stat-card">
      <div class="label">Total Devices</div>
      <div class="value accent">{stats['total']}</div>
      <div class="sub">{len(by_type)} group{"s" if len(by_type)!=1 else ""}</div>
    </div>
    <div class="stat-card">
      <div class="label">Responding</div>
      <div class="value up">{stats['up']}</div>
      <div class="sub">{pct_up}% of total</div>
    </div>
    <div class="stat-card">
      <div class="label">Not Responding</div>
      <div class="value down">{stats['down']}</div>
      <div class="sub">{100-pct_up}% of total</div>
    </div>
    <div class="stat-card">
      <div class="label">Last Sweep</div>
      <div class="value" style="font-size:.95rem;color:var(--text)">{last_t}</div>
      <div class="sub">completed</div>
    </div>
    <div class="stat-card">
      <div class="label">Next Sweep</div>
      <div class="value" style="font-size:.95rem;color:var(--text)">{next_t}</div>
      <div class="sub">scheduled</div>
    </div>
  </div>

  {'<div style="display:flex;justify-content:flex-end;margin-bottom:1rem"><a class="btn btn-ghost btn-sm" href="/export.csv">⬇ Export All (CSV)</a></div>' if stats['total'] else ""}

  {"<p class='section-title'>Device Group Health</p><div class='health-bars'>" + bars_html + "</div>" if bars_html else ""}
</main>
{render_footer("auto-refreshes every sweep cycle")}
<script>setTimeout(()=>location.reload(),{interval*1000});</script>
</body></html>""")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    error = None
    next_url = request.values.get("next") or url_for("dashboard")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"]              = user["id"]
            session["username"]             = user["username"]
            session["role"]                 = user["role"]
            session["must_change_password"] = bool(user["must_change_password"])
            touch_last_login(user["id"])
            log.info("User logged in: %s (%s)", user["username"], user["role"])
            return redirect(request.form.get("next") or next_url)
        error = "Incorrect username or password."
        log.warning("Failed login attempt for username: %s", username)

    alert = f'<div class="alert alert-error">{error}</div>' if error else ""
    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — Log In</title>{BASE_STYLE}</head><body>
{render_header()}
<main class="auth-wrap">
  <div class="form-card">
    <div class="section-title">Log In</div>
    {alert}
    <form method="POST" action="/login">
      <input type="hidden" name="next" value="{next_url}">
      <div class="form-group"><label class="form-label">Username</label>
        <input class="form-input" name="username" autofocus required></div>
      <div class="form-group"><label class="form-label">Password</label>
        <input class="form-input" type="password" name="password" required></div>
      <div class="form-actions"><button class="btn btn-primary" type="submit">Log In</button></div>
    </form>
  </div>
</main>
{render_footer()}
</body></html>""")


@app.route("/logout")
def logout():
    if session.get("username"):
        log.info("User logged out: %s", session.get("username"))
    session.clear()
    return redirect(url_for("dashboard"))


@app.route("/account/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    error  = None
    forced = bool(session.get("must_change_password"))

    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw     = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")
        user = get_user_by_id(session["user_id"])
        if not user or not check_password_hash(user["password_hash"], current_pw):
            error = "Current password is incorrect."
        elif len(new_pw) < 8:
            error = "New password must be at least 8 characters."
        elif new_pw != confirm_pw:
            error = "New password and confirmation do not match."
        elif new_pw == current_pw:
            error = "New password must be different from the current password."
        else:
            reset_user_password(user["id"], new_pw, force_change=False)
            session["must_change_password"] = False
            log.info("Password changed for user: %s", user["username"])
            return redirect(url_for("dashboard"))

    notice = ""
    if forced:
        notice = ('<div class="alert alert-warn">You are signing in with a default or temporary '
                   'password. Please set a new password to continue.</div>')
    alert = f'<div class="alert alert-error">{error}</div>' if error else ""

    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — Change Password</title>{BASE_STYLE}</head><body>
{render_header()}
<main class="auth-wrap">
  <div class="form-card">
    <div class="section-title">Change Password</div>
    {notice}{alert}
    <form method="POST" action="/account/change-password">
      <div class="form-group"><label class="form-label">Current Password</label>
        <input class="form-input" type="password" name="current_password" required></div>
      <div class="form-group"><label class="form-label">New Password</label>
        <input class="form-input" type="password" name="new_password" required></div>
      <div class="form-group"><label class="form-label">Confirm New Password</label>
        <input class="form-input" type="password" name="confirm_password" required></div>
      <div class="form-hint">Minimum 8 characters.</div>
      <div class="form-actions"><button class="btn btn-primary" type="submit">Update Password</button></div>
    </form>
  </div>
</main>
{render_footer()}
</body></html>""")


def _devices_page(device_type, error=None):
    dt   = device_type.upper()
    rows = _filter_to_configured(query_latest_per_host(dt))
    up   = sum(1 for r in rows if r["status"] == 1)
    down = len(rows) - up
    user = current_user()
    col_count = 7 if user else 6

    rows_html = ""
    for r in rows:
        ip       = r.get("ip_address") or r.get("hostname") or ""
        hostname = r.get("hostname", "")
        location = r.get("location", "")
        latency  = r.get("latency_ms")
        lat_str  = f"{latency} ms" if latency is not None else "—"
        pinged   = r.get("pinged_at", "")
        badge    = f'<span class="badge {"up" if r["status"] else "down"}">{"UP" if r["status"] else "DOWN"}</span>'
        ip_link  = f'<a href="http://{ip}" target="_blank" rel="noopener">{ip}</a>' if ip else "—"
        hist_url = f"/history/{hostname}"

        actions = ""
        if user:
            actions = f"""
            <td class="actions">
              <a class="btn btn-ghost btn-sm" href="/devices/{dt}/edit/{hostname}">Edit</a>
              <form class="inline-form" method="POST" action="/devices/{dt}/delete/{hostname}"
                    onsubmit="return confirm('Remove {hostname} from {dt}?');">
                <button class="btn btn-danger btn-sm" type="submit">Delete</button>
              </form>
            </td>"""

        rows_html += f"""
          <tr>
            <td class="hostname"><a href="{hist_url}">{hostname}</a></td>
            <td class="ip">{ip_link}</td>
            <td class="location">{location}</td>
            <td>{badge}</td>
            <td class="latency">{lat_str}</td>
            <td class="timestamp">{pinged}</td>
            {actions}
          </tr>"""

    table_body = rows_html if rows_html else f'<tr><td colspan="{col_count}" class="empty-state">No devices currently in this group.</td></tr>'
    actions_header = "<th>Actions</th>" if user else ""
    alert = f'<div class="alert alert-error">{error}</div>' if error else ""

    add_form = ""
    if user:
        add_form = f"""
  <div class="form-card" style="margin-top:1.5rem">
    <div class="section-title">Add Device to {dt}</div>
    <form method="POST" action="/devices/{dt}/add">
      <div class="form-group"><label class="form-label">Hostname / IP</label>
        <input class="form-input" name="hostname" required></div>
      <div class="form-group"><label class="form-label">Location</label>
        <input class="form-input" name="location" placeholder="optional"></div>
      <div class="form-actions"><button class="btn btn-primary" type="submit">Add Device</button></div>
    </form>
  </div>"""

    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — {dt}</title>{BASE_STYLE}</head><body>
{render_header()}{render_tabs(dt)}
<main>
  {alert}
  <div class="stat-grid" style="margin-bottom:1.5rem">
    <div class="stat-card"><div class="label">Total</div><div class="value accent">{len(rows)}</div></div>
    <div class="stat-card"><div class="label">Up</div><div class="value up">{up}</div></div>
    <div class="stat-card"><div class="label">Down</div><div class="value down">{down}</div></div>
  </div>
  <div class="table-wrapper">
    <div class="table-toolbar">
      <div class="toolbar-left">
        <div class="toolbar-title">{dt}</div>
      </div>
      <div style="display:flex;align-items:center;gap:.6rem">
        <input class="search-box" id="fb" type="text" placeholder="Filter…" oninput="filterTable()">
        <a class="btn btn-ghost btn-sm" href="/devices/{dt}/export.csv">⬇ Export CSV</a>
      </div>
    </div>
    <table id="dt">
      <thead><tr>
        <th onclick="sortTable(0)">Hostname <i class="si">⇅</i></th>
        <th onclick="sortTable(1)">IP Address <i class="si">⇅</i></th>
        <th onclick="sortTable(2)">Location <i class="si">⇅</i></th>
        <th onclick="sortTable(3)">Status <i class="si">⇅</i></th>
        <th onclick="sortTable(4)">Latency <i class="si">⇅</i></th>
        <th onclick="sortTable(5)">Last Seen <i class="si">⇅</i></th>
        {actions_header}
      </tr></thead>
      <tbody>{table_body}</tbody>
    </table>
  </div>
  {add_form}
</main>
{render_footer("click a hostname to view its uptime history")}
{TABLE_SCRIPT}</body></html>""")


@app.route("/devices/<device_type>")
def devices(device_type):
    return _devices_page(device_type.upper())


def _csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_row(r, include_group=False):
    row = [r.get("device_type", "")] if include_group else []
    row += [
        r.get("hostname", ""),
        r.get("ip_address", "") or "",
        r.get("location", "") or "",
        "UP" if r.get("status") == 1 else "DOWN",
        r.get("latency_ms") if r.get("latency_ms") is not None else "",
        r.get("pinged_at", ""),
    ]
    return row


@app.route("/devices/<device_type>/export.csv")
def device_export_csv(device_type):
    dt   = device_type.upper()
    rows = _filter_to_configured(query_latest_per_host(dt))
    header = ["Hostname", "IP Address", "Location", "Status", "Latency (ms)", "Last Seen"]
    data   = [_csv_row(r) for r in rows]
    stamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _csv_response(f"pingmon_{dt}_{stamp}.csv", header, data)


@app.route("/export.csv")
def export_all_csv():
    rows   = _filter_to_configured(query_latest_per_host())
    header = ["Device Group", "Hostname", "IP Address", "Location", "Status", "Latency (ms)", "Last Seen"]
    data   = [_csv_row(r, include_group=True) for r in rows]
    stamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _csv_response(f"pingmon_all_devices_{stamp}.csv", header, data)


@app.route("/devices/<device_type>/add", methods=["POST"])
@login_required
def device_add(device_type):
    dt = device_type.upper()
    try:
        add_device_to_group(dt, request.form.get("hostname", ""), request.form.get("location", ""))
    except ValueError as e:
        return _devices_page(dt, error=str(e))
    return redirect(url_for("devices", device_type=dt))


@app.route("/devices/<device_type>/edit/<hostname>", methods=["GET", "POST"])
@login_required
def device_edit(device_type, hostname):
    dt = device_type.upper()
    error = None

    if request.method == "POST":
        try:
            edit_device_in_group(dt, hostname,
                                  request.form.get("hostname", ""),
                                  request.form.get("location", ""))
            return redirect(url_for("devices", device_type=dt))
        except ValueError as e:
            error = str(e)

    rows = query_latest_per_host(dt)
    current = next((r for r in rows if r["hostname"] == hostname), None)
    cur_location = current.get("location", "") if current else ""

    alert = f'<div class="alert alert-error">{error}</div>' if error else ""
    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — Edit Device</title>{BASE_STYLE}</head><body>
{render_header()}{render_tabs(dt)}
<main style="max-width:520px">
  <a class="back-link" href="/devices/{dt}">← Back to {dt}</a>
  <div class="form-card">
    <div class="section-title">Edit Device — {hostname}</div>
    {alert}
    <form method="POST" action="/devices/{dt}/edit/{hostname}">
      <div class="form-group"><label class="form-label">Hostname / IP</label>
        <input class="form-input" name="hostname" value="{hostname}" required></div>
      <div class="form-group"><label class="form-label">Location</label>
        <input class="form-input" name="location" value="{cur_location}"></div>
      <div class="form-actions">
        <button class="btn btn-primary" type="submit">Save Changes</button>
        <a class="btn btn-ghost" href="/devices/{dt}">Cancel</a>
      </div>
    </form>
  </div>
</main>
{render_footer()}
</body></html>""")


@app.route("/devices/<device_type>/delete/<hostname>", methods=["POST"])
@login_required
def device_delete(device_type, hostname):
    dt = device_type.upper()
    try:
        delete_device_from_group(dt, hostname)
    except ValueError as e:
        return _devices_page(dt, error=str(e))
    return redirect(url_for("devices", device_type=dt))


@app.route("/admin/groups/new", methods=["GET", "POST"])
@admin_required
def group_new():
    error = None
    if request.method == "POST":
        try:
            path = create_group_file(request.form.get("name", ""))
            return redirect(url_for("devices", device_type=path.stem))
        except ValueError as e:
            error = str(e)

    alert = f'<div class="alert alert-error">{error}</div>' if error else ""
    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — New Device Group</title>{BASE_STYLE}</head><body>
{render_header()}{render_tabs("new-group")}
<main class="auth-wrap">
  <div class="form-card">
    <div class="section-title">New Device Group</div>
    {alert}
    <form method="POST" action="/admin/groups/new">
      <div class="form-group">
        <label class="form-label">Group Name</label>
        <input class="form-input" name="name" placeholder="e.g. ROUTERS" required>
        <div class="form-hint">Becomes ROUTERS.DAT. It won't appear as a dashboard tab until the
          next ping sweep includes at least one device — add one below once created.</div>
      </div>
      <div class="form-actions">
        <button class="btn btn-primary" type="submit">Create Group</button>
        <a class="btn btn-ghost" href="/">Cancel</a>
      </div>
    </form>
  </div>
</main>
{render_footer()}
</body></html>""")


def _users_page(error=None, success=None):
    users = list_users()
    me = session.get("user_id")

    rows_html = ""
    for u in users:
        role_cls   = "admin" if u["role"] == "admin" else "user"
        role_label = "Administrator" if u["role"] == "admin" else "User"

        if u["id"] == me:
            role_cell = f'<span class="role-badge {role_cls}">{role_label}</span>'
            delete_btn = ""
        else:
            role_cell = f"""
              <form class="inline-form" method="POST" action="/admin/users/{u['id']}/role">
                <select class="form-select" name="role" onchange="this.form.submit()">
                  <option value="user" {"selected" if u['role']=="user" else ""}>User</option>
                  <option value="admin" {"selected" if u['role']=="admin" else ""}>Administrator</option>
                </select>
              </form>"""
            delete_btn = f"""
              <form class="inline-form" method="POST" action="/admin/users/{u['id']}/delete"
                    onsubmit="return confirm('Delete user {u['username']}?');">
                <button class="btn btn-danger btn-sm" type="submit">Delete</button>
              </form>"""

        pending = '<span class="alert-inline">must change password</span>' if u["must_change_password"] else "—"
        last_login = u.get("last_login") or "never"
        self_tag = " (you)" if u["id"] == me else ""

        rows_html += f"""
          <tr>
            <td class="hostname">{u['username']}{self_tag}</td>
            <td>{role_cell}</td>
            <td class="timestamp">{u['created_at']}</td>
            <td class="timestamp">{last_login}</td>
            <td>{pending}</td>
            <td class="actions">
              <form class="inline-form" method="POST" action="/admin/users/{u['id']}/reset-password"
                    onsubmit="return confirm('Reset password for {u['username']} to a new temporary password?');">
                <button class="btn btn-ghost btn-sm" type="submit">Reset Password</button>
              </form>
              {delete_btn}
            </td>
          </tr>"""

    alert = ""
    if error:
        alert = f'<div class="alert alert-error">{error}</div>'
    elif success:
        alert = f'<div class="alert alert-success">{success}</div>'

    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — Users</title>{BASE_STYLE}</head><body>
{render_header()}{render_tabs("users")}
<main>
  {alert}
  <div class="table-wrapper" style="margin-bottom:1.5rem">
    <div class="table-toolbar"><div class="toolbar-title">User Accounts</div></div>
    <table id="ut">
      <thead><tr>
        <th>Username</th><th>Role</th><th>Created</th><th>Last Login</th><th>Status</th><th>Actions</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <div class="form-card">
    <div class="section-title">Create New User</div>
    <form method="POST" action="/admin/users/new">
      <div class="form-group"><label class="form-label">Username</label>
        <input class="form-input" name="username" required></div>
      <div class="form-group"><label class="form-label">Temporary Password</label>
        <input class="form-input" type="password" name="password" required></div>
      <div class="form-group">
        <label class="form-label">Role</label>
        <select class="form-select" name="role">
          <option value="user">User — can edit existing device groups</option>
          <option value="admin">Administrator — can also create new device groups and manage users</option>
        </select>
      </div>
      <div class="form-hint">The new user will be required to set their own password on first login.</div>
      <div class="form-actions"><button class="btn btn-primary" type="submit">Create User</button></div>
    </form>
  </div>
</main>
{render_footer()}
</body></html>""")


@app.route("/admin/users")
@admin_required
def users_admin():
    return _users_page()


@app.route("/admin/users/new", methods=["POST"])
@admin_required
def user_new():
    try:
        create_user(request.form.get("username", ""),
                    request.form.get("password", ""),
                    request.form.get("role", "user"))
    except ValueError as e:
        return _users_page(error=str(e))
    return redirect(url_for("users_admin"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def user_role(user_id):
    if user_id == session.get("user_id"):
        return _users_page(error="You cannot change your own role.")
    new_role = request.form.get("role", "user")
    target = get_user_by_id(user_id)
    if not target:
        return _users_page(error="That user no longer exists.")
    if new_role != "admin" and target["role"] == "admin" and count_admins() <= 1:
        return _users_page(error="At least one Administrator account must remain.")
    try:
        set_user_role(user_id, new_role)
    except ValueError as e:
        return _users_page(error=str(e))
    return redirect(url_for("users_admin"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def user_reset_password(user_id):
    target = get_user_by_id(user_id)
    if not target:
        return _users_page(error="That user no longer exists.")
    temp_password = secrets.token_urlsafe(9)
    reset_user_password(user_id, temp_password, force_change=True)
    log.info("Password reset by administrator for user: %s", target["username"])
    return _users_page(success=(
        f'Temporary password for "{target["username"]}": {temp_password} — '
        f'share this with them securely. They will be asked to set a new password at next login.'
    ))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def user_delete(user_id):
    if user_id == session.get("user_id"):
        return _users_page(error="You cannot delete your own account.")
    target = get_user_by_id(user_id)
    if not target:
        return _users_page(error="That user no longer exists.")
    if target["role"] == "admin" and count_admins() <= 1:
        return _users_page(error="At least one Administrator account must remain.")
    delete_user(user_id)
    log.info("User deleted by administrator: %s", target["username"])
    return redirect(url_for("users_admin"))


@app.route("/history/<hostname>")
def history(hostname):
    hours = int(request.args.get("hours", 24))
    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — {hostname}</title>{BASE_STYLE}</head><body>
{render_header()}
<main style="max-width:1100px">
  <a class="back-link" href="javascript:history.back()">← Back</a>
  <div class="stat-grid" style="margin-bottom:1.5rem" id="summaryCards">
    <div class="stat-card"><div class="label">Device</div>
      <div class="value" style="font-size:1rem;color:var(--text)">{hostname}</div></div>
    <div class="stat-card"><div class="label">Uptime %</div>
      <div class="uptime-big" id="uptimePct">—</div>
      <div class="uptime-lbl" id="uptimeLbl"></div></div>
    <div class="stat-card"><div class="label">Avg Latency</div>
      <div class="value accent" id="avgLatency">—</div>
      <div class="sub">milliseconds</div></div>
    <div class="stat-card"><div class="label">Checks</div>
      <div class="value accent" id="checkCount">—</div>
      <div class="sub" id="checkWindow"></div></div>
  </div>

  <div class="range-btns">
    <button class="range-btn {"active" if hours==1  else ""}" onclick="loadHistory(1)">1 h</button>
    <button class="range-btn {"active" if hours==6  else ""}" onclick="loadHistory(6)">6 h</button>
    <button class="range-btn {"active" if hours==24 else ""}" onclick="loadHistory(24)">24 h</button>
    <button class="range-btn {"active" if hours==72 else ""}" onclick="loadHistory(72)">3 d</button>
    <button class="range-btn {"active" if hours==168 else ""}" onclick="loadHistory(168)">7 d</button>
    <button class="range-btn {"active" if hours==720 else ""}" onclick="loadHistory(720)">30 d</button>
  </div>

  <div class="chart-card">
    <h3>Status Timeline</h3>
    <canvas id="statusChart" height="90"></canvas>
  </div>

  <div class="chart-card">
    <h3>Latency (ms)</h3>
    <canvas id="latencyChart" height="120"></canvas>
  </div>
</main>
{render_footer()}

<script>
const HOSTNAME = {hostname!r};
let currentHours = {hours};
let statusChart  = null;
let latencyChart = null;

function loadHistory(hours){{
  currentHours = hours;
  document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  window.history.replaceState(null,'',`/history/${{HOSTNAME}}?hours=${{hours}}`);
  fetchAndRender(hours);
}}

async function fetchAndRender(hours){{
  const res  = await fetch(`/api/history/${{HOSTNAME}}?hours=${{hours}}`);
  const data = await res.json();

  const total   = data.length;
  const upCount = data.filter(d=>d.status===1).length;
  const pct     = total ? ((upCount/total)*100).toFixed(1) : '—';
  const lats    = data.filter(d=>d.latency_ms!=null).map(d=>d.latency_ms);
  const avgLat  = lats.length ? (lats.reduce((a,b)=>a+b,0)/lats.length).toFixed(1) : '—';

  document.getElementById('uptimePct').textContent = pct !== '—' ? pct+'%' : '—';
  document.getElementById('uptimePct').style.color = pct>=99 ? 'var(--up)' : pct>=90 ? 'var(--warn)' : 'var(--down)';
  document.getElementById('uptimeLbl').textContent = `${{upCount}} / ${{total}} checks`;
  document.getElementById('avgLatency').textContent = avgLat !== '—' ? avgLat : '—';
  document.getElementById('checkCount').textContent = total;
  document.getElementById('checkWindow').textContent = `last ${{hours}} hours`;

  const labels   = data.map(d => d.pinged_at);
  const statuses = data.map(d => d.status);
  const latencies= data.map(d => d.latency_ms);

  // Read theme colours from CSS variables so charts match whichever theme is active
  const cs      = getComputedStyle(document.documentElement);
  const cGrid   = cs.getPropertyValue('--chart-grid').trim()   || 'rgba(128,128,128,.1)';
  const cBorder = cs.getPropertyValue('--chart-border').trim() || 'rgba(128,128,128,.2)';
  const cTick   = cs.getPropertyValue('--chart-tick').trim()   || 'rgba(128,128,128,.6)';
  const cAccent = cs.getPropertyValue('--accent').trim()       || '#00e5ff';

  // Status chart
  if(statusChart) statusChart.destroy();
  const sCtx = document.getElementById('statusChart').getContext('2d');
  statusChart = new Chart(sCtx, {{
    type: 'bar',
    data: {{
      labels,
      datasets:[{{
        label: 'Status (1=UP, 0=DOWN)',
        data: statuses,
        backgroundColor: statuses.map(s => s===1 ? 'rgba(34,197,94,.65)' : 'rgba(239,68,68,.65)'),
        borderWidth: 0,
        borderRadius: 2,
      }}]
    }},
    options:{{
      responsive:true, animation:false,
      scales:{{
        x:{{display:false}},
        y:{{min:0,max:1,
           ticks:{{stepSize:1,callback:v=>v?'UP':'DOWN',color:cTick,font:{{size:11}}}},
           grid:{{color:cGrid}},border:{{color:cBorder}}}},
      }},
      plugins:{{legend:{{display:false}},
        tooltip:{{callbacks:{{
          label: ctx => ctx.raw===1 ? 'UP' : 'DOWN',
          title: items => items[0].label
        }}}}
      }}
    }}
  }});

  // Latency chart — compute a translucent version of the accent colour for the fill
  let cAccentBg = 'rgba(0,229,255,.07)';
  try {{
    const h = cAccent.replace('#','');
    if(h.length===6) cAccentBg = 'rgba('+parseInt(h.slice(0,2),16)+','+parseInt(h.slice(2,4),16)+','+parseInt(h.slice(4,6),16)+',.07)';
  }} catch(e) {{}}
  if(latencyChart) latencyChart.destroy();
  const lCtx = document.getElementById('latencyChart').getContext('2d');
  latencyChart = new Chart(lCtx, {{
    type: 'line',
    data: {{
      labels,
      datasets:[{{
        label: 'Latency ms',
        data: latencies,
        borderColor: cAccent,
        backgroundColor: cAccentBg,
        borderWidth: 1.5,
        pointRadius: data.length > 200 ? 0 : 2,
        pointHoverRadius: 4,
        fill: true,
        tension: 0.3,
        spanGaps: false,
      }}]
    }},
    options:{{
      responsive:true, animation:false,
      scales:{{
        x:{{display:false}},
        y:{{min:0,
           grid:{{color:cGrid}},border:{{color:cBorder}},
           ticks:{{color:cTick,font:{{size:11}}}}}},
      }},
      plugins:{{
        legend:{{display:false}},
        tooltip:{{callbacks:{{
          label: ctx => ctx.raw != null ? ctx.raw+' ms' : 'no data',
          title: items => items[0].label
        }}}}
      }}
    }}
  }});
}}

// Load on page open
fetchAndRender(currentHours);
</script>
</body></html>""")


@app.route("/api/history/<hostname>")
def api_history(hostname):
    hours = int(request.args.get("hours", 24))
    rows  = query_device_history(hostname, hours)
    return jsonify(rows)


@app.route("/api/stats")
def api_stats():
    stats = query_dashboard_stats()
    return jsonify({
        **stats,
        "last_sweep": last_sweep_time.isoformat() if last_sweep_time else None,
        "next_sweep": next_sweep_time.isoformat() if next_sweep_time else None,
    })

# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    load_config()
    app.secret_key = cfg["secret_key"]
    init_db()

    t = threading.Thread(target=sweep_loop, daemon=True, name="SweepLoop")
    t.start()

    port    = cfg.get("port",    9090)
    threads = cfg.get("threads", 8)
    log.info("Starting Waitress on http://0.0.0.0:%d  (threads=%d)", port, threads)
    serve(app, host="0.0.0.0", port=port, threads=threads)

if __name__ == "__main__":
    main()
