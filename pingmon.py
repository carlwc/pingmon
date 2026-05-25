#!/usr/bin/env python3
"""
PingMon v2 — Asynchronous Network Device Monitor
Backend : asyncio ping sweep + SQLite
Frontend: Flask + Waitress (cross-platform, multi-threaded)
"""

import asyncio
import configparser
import logging
import os
import random
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, request
from waitress import serve

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
BASE_DIR    = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "pingmon.conf"
DB_FILE     = BASE_DIR / "pingmon.db"

# ── Shared state ──────────────────────────────────────────────────────────────
cfg            = {}
last_sweep_time = None
next_sweep_time = None
state_lock      = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

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
        """)
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
    latest = query_latest_per_host()
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
# INPUT FILE PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_txt_file(path: Path):
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

def discover_txt_files():
    result = {}
    seen = set()
    patterns = list(BASE_DIR.glob("*.txt")) + list(BASE_DIR.glob("*.TXT"))
    for txt in sorted(set(patterns)):
        key = txt.stem.upper()
        if key in seen:
            continue
        seen.add(key)
        devices = parse_txt_file(txt)
        if devices:
            result[key] = devices
            log.info("Loaded %d devices from %s", len(devices), txt.name)
    return result

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
        devices = discover_txt_files()
        if devices:
            loop.run_until_complete(run_sweep(devices))
        else:
            log.warning("No device .TXT files found in %s", BASE_DIR)
        time.sleep(cfg.get("interval_seconds", 300))

# ═══════════════════════════════════════════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

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

def render_header():
    return """
<header>
  <div class="header-left">
    <div>
      <div class="logo">Ping<span>Mon</span></div>
      <div class="tagline">Network Device Monitor v2</div>
    </div>
  </div>
  <button id="themeBtn" onclick="_pmToggleTheme()" title="Toggle Day / Night theme">
    <span class="icon">☀️</span>
    <span class="label">Day Mode</span>
  </button>
</header>"""

def render_tabs(active):
    device_types = get_device_types()
    html = '<nav class="tab-bar">'
    html += f'<a href="/" class="{"active" if active=="dashboard" else ""}">📊 Dashboard</a>'
    for dt in device_types:
        cls = "active" if active == dt else ""
        html += f'<a href="/devices/{dt}" class="{cls}">{dt.title()}</a>'
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

  {"<p class='section-title'>Device Group Health</p><div class='health-bars'>" + bars_html + "</div>" if bars_html else ""}
</main>
<footer>PingMon v2 &mdash; auto-refreshes every sweep cycle</footer>
<script>setTimeout(()=>location.reload(),{interval*1000});</script>
</body></html>""")


@app.route("/devices/<device_type>")
def devices(device_type):
    dt   = device_type.upper()
    rows = query_latest_per_host(dt)
    up   = sum(1 for r in rows if r["status"] == 1)
    down = len(rows) - up

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
        rows_html += f"""
          <tr>
            <td class="hostname"><a href="{hist_url}">{hostname}</a></td>
            <td class="ip">{ip_link}</td>
            <td class="location">{location}</td>
            <td>{badge}</td>
            <td class="latency">{lat_str}</td>
            <td class="timestamp">{pinged}</td>
          </tr>"""

    table_body = rows_html if rows_html else '<tr><td colspan="6" class="empty-state">No data yet — waiting for first sweep…</td></tr>'

    return render_template_string(f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PingMon — {dt}</title>{BASE_STYLE}</head><body>
{render_header()}{render_tabs(dt)}
<main>
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
      <input class="search-box" id="fb" type="text" placeholder="Filter…" oninput="filterTable()">
    </div>
    <table id="dt">
      <thead><tr>
        <th onclick="sortTable(0)">Hostname <i class="si">⇅</i></th>
        <th onclick="sortTable(1)">IP Address <i class="si">⇅</i></th>
        <th onclick="sortTable(2)">Location <i class="si">⇅</i></th>
        <th onclick="sortTable(3)">Status <i class="si">⇅</i></th>
        <th onclick="sortTable(4)">Latency <i class="si">⇅</i></th>
        <th onclick="sortTable(5)">Last Seen <i class="si">⇅</i></th>
      </tr></thead>
      <tbody>{table_body}</tbody>
    </table>
  </div>
</main>
<footer>PingMon v2 &mdash; click a hostname to view its uptime history</footer>
{TABLE_SCRIPT}</body></html>""")


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
<footer>PingMon v2</footer>

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
    init_db()

    t = threading.Thread(target=sweep_loop, daemon=True, name="SweepLoop")
    t.start()

    port    = cfg.get("port",    9090)
    threads = cfg.get("threads", 8)
    log.info("Starting Waitress on http://0.0.0.0:%d  (threads=%d)", port, threads)
    serve(app, host="0.0.0.0", port=port, threads=threads)

if __name__ == "__main__":
    main()
