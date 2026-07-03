# PingMon v3 — Network Device Monitor

A self-contained Python application that pings network devices on a schedule,
stores results in SQLite, and serves a live multi-user web dashboard via
Flask + Waitress (cross-platform: Windows, macOS, Linux).

---

## Folder Layout

```
pingmon/
├── pingmon.py           ← Entire application
├── pingmon.conf         ← Configuration
├── pingmon.secret       ← Session signing key (auto-created on first run)
├── requirements.txt     ← pip dependencies (flask, waitress)
├── pingmon.db           ← SQLite database (auto-created on first run — also holds user accounts)
├── SWITCHES.DAT         ← Device list — network switches
├── UPS.DAT              ← Device list — UPS units
├── LASERPRINTERS.DAT    ← Device list — laser printers
├── ZEBRAPRINTERS.DAT    ← Device list — Zebra label printers
└── README.md
```

Any `.DAT` file in this folder is treated as a device group.
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
pip install -r requirements.txt

# macOS / Linux
pip3 install -r requirements.txt
```

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

## Device List Format (.DAT files)

```
! Lines starting with ! are comments — ignored
! Blank lines are also ignored

Hostname-or-IP, Physical location description
192.168.1.1,    Core switch, MDF Closet
myrouter.local, Edge router, Server Room Rack 3
```

Drop a new `.DAT` file in the folder and it will appear as a new tab
on the next sweep — no restart needed.

---

## Authentication & User Accounts

Viewing the dashboard, device tables, and history pages never requires a
login — that's unchanged from before. A login is only required to **add,
edit, or delete devices**, **create a new device group**, or **manage user
accounts**.

There are two roles:

| Role              | Can do                                                                 |
|--------------------|-------------------------------------------------------------------------|
| **User**            | Add, edit, and delete devices within existing device groups.          |
| **Administrator**   | Everything a User can do, plus: create new device groups (`.DAT` files) and create/edit/delete user accounts. |

**Default account:** on first run, PingMon creates one Administrator
account automatically — username `admin`, password `admin`. You will be
required to set a new password immediately after logging in with it for
the first time; PingMon will not let you browse away until you do.

Administrators create additional accounts from the **Users** tab
(`/admin/users`). Every new account — including ones created by an
Administrator — is required to set its own password at first login, the
same way the default `admin` account is.

Session cookies are signed with a random key stored in `pingmon.secret`,
generated automatically the first time PingMon runs. Keep this file
private and don't commit it to source control — anyone with it can forge
a session cookie. Deleting it invalidates all logged-in sessions.

### Where accounts are stored

User accounts live in a `users` table inside `pingmon.db` — the same
database file as your ping history. There is no separate accounts file.

### Resetting accounts to the default state

To wipe all user accounts and restore the single default `admin` / `admin`
account (with the forced password change re-enabled), stop PingMon, clear
the `users` table only (this does **not** touch your `ping_results`
history), then restart:

```bash
sqlite3 pingmon.db "DELETE FROM users;"
```

or run the same `DELETE FROM users;` statement in any SQLite browser
(e.g. DB Browser for SQLite). On the next start, PingMon detects that the
`users` table is empty and automatically re-creates `admin` / `admin`,
just like a first-ever run.

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

| Page                      | URL                              | Login required |
|---------------------------|-----------------------------------|:---:|
| Dashboard                 | http://localhost:9090/           | No |
| Device group tab          | http://localhost:9090/devices/SWITCHES | No |
| Per-device history        | http://localhost:9090/history/hostname | No |
| History JSON API          | http://localhost:9090/api/history/hostname?hours=24 | No |
| Dashboard stats JSON      | http://localhost:9090/api/stats  | No |
| Export a group to CSV     | http://localhost:9090/devices/SWITCHES/export.csv | No |
| Export all devices to CSV | http://localhost:9090/export.csv | No |
| Log in / Log out          | http://localhost:9090/login · /logout | — |
| Change your password      | http://localhost:9090/account/change-password | Yes |
| Add / edit / delete a device | (buttons on the device group tab) | Yes |
| Create a new device group | http://localhost:9090/admin/groups/new | Administrator |
| Manage user accounts      | http://localhost:9090/admin/users | Administrator |

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
Description=PingMon v3
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
