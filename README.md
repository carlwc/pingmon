# PingMon v2 — Network Device Monitor

A self-contained Python application that pings network devices on a schedule,
stores results in SQLite, and serves a live multi-user web dashboard via
Flask + Waitress (cross-platform: Windows, macOS, Linux).

---

## Folder Layout

```
pingmon/
├── pingmon.py           ← Entire application
├── pingmon.conf         ← Configuration
├── requirements.txt     ← pip dependencies (flask, waitress)
├── pingmon.db           ← SQLite database (auto-created on first run)
├── SWITCHES.TXT         ← Device list — network switches
├── UPS.TXT              ← Device list — UPS units
├── LASERPRINTERS.TXT    ← Device list — laser printers
├── ZEBRAPRINTERS.TXT    ← Device list — Zebra label printers
└── README.md
```

Any `.TXT` file in this folder is treated as a device group.
The filename (without extension) becomes the tab name in the dashboard.

---

## Requirements

- Python 3.8 or newer
- `ping` in PATH (present by default on Windows, macOS, Linux)
- Flask + Waitress (install once with pip)

---

## Installation (one-time)

```bash
# Windows
pip install -r requirements.txt-

# macOS / Linux
pip3 install -r requirements.txt-
```

Note: The file extension has an attached dash to keep it separate from the main app's 
input files. I'll fix this in an upcoming release.


---

## Running

```bash
# Windows
python pingmon.py

# macOS / Linux
python3 pingmon.py
```

Then open:  http://localhost:9090

---

## Device List Format (.TXT files)

```
! Lines starting with ! are comments — ignored
! Blank lines are also ignored

Hostname-or-IP, Physical location description
192.168.1.1,    Core switch, MDF Closet
myrouter.local, Edge router, Server Room Rack 3
```

Drop a new `.TXT` file in the folder and it will appear as a new tab
on the next sweep — no restart needed.

---

## Configuration (pingmon.conf)

| Section    | Key                  | Default | Description                                  |
|------------|----------------------|---------|----------------------------------------------|
| [server]   | port                 | 9090    | Web server port                              |
| [server]   | threads              | 8       | Waitress worker threads (concurrent clients) |
| [ping]     | interval_seconds     | 300     | Seconds between sweeps                       |
| [ping]     | timeout_seconds      | 2       | Per-ping timeout                             |
| [ping]     | count                | 1       | ICMP packets per host                        |
| [ping]     | jitter_max_seconds   | 8       | Max random delay before each ping            |
| [database] | file                 | pingmon.db | SQLite path                               |

---

## Web Interface

| Page                      | URL                              |
|---------------------------|----------------------------------|
| Dashboard                 | http://localhost:9090/           |
| Device group tab          | http://localhost:9090/devices/SWITCHES |
| Per-device history        | http://localhost:9090/history/hostname |
| History JSON API          | http://localhost:9090/api/history/hostname?hours=24 |
| Dashboard stats JSON      | http://localhost:9090/api/stats  |

### History page features
- **Time range selector:** 1h · 6h · 24h · 3d · 7d · 30d
- **Status timeline chart:** green=UP, red=DOWN bars per poll
- **Latency chart:** millisecond response time over the selected window
- **Uptime %**, average latency, and check count summary cards

---

## Running as a Background Service

### Windows (Task Scheduler)
Create a Basic Task → trigger: At Startup → action:
`python C:\path\to\pingmon\pingmon.py`

### Linux (systemd)
```ini
[Unit]
Description=PingMon v2
After=network.target

[Service]
WorkingDirectory=/opt/pingmon
ExecStart=/usr/bin/python3 /opt/pingmon/pingmon.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now pingmon
```

### macOS (launchd)
Create `~/Library/LaunchAgents/com.pingmon.plist` with a standard
`ProgramArguments` entry pointing to `python3 /path/to/pingmon.py`.

---

## Database Maintenance

PingMon keeps every historical ping. To prune old data:

```sql
-- keep only the last 90 days
DELETE FROM ping_results WHERE pinged_at < date('now', '-90 days');
VACUUM;
```

Run this with any SQLite browser (e.g. DB Browser for SQLite).
