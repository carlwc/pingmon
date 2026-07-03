# Changelog

## v3.0.0 — 2026-07-02

A baseline PRD reconstructing PingMon v2's actual shipped behavior was
produced first (see project history); everything below is new relative
to that baseline.

### Added — User Accounts & Authentication
- Session-based login system. Viewing the dashboard, device tables,
  history pages, and CSV exports still requires **no login** — only
  editing devices, creating device groups, and managing user accounts
  now require it.
- Two roles: **User** (add/edit/delete devices within existing groups)
  and **Administrator** (everything a User can do, plus create new
  device groups and manage user accounts). Administrator inherits all
  User capabilities.
- A default `admin` / `admin` Administrator account is created
  automatically on first run. Logging in with it — or with any newly
  created account — forces an immediate password change before any
  other page can be reached.
- Administrator-only user management page (`/admin/users`): create
  accounts, change roles, reset passwords, delete accounts. Guarded
  against locking yourself out — you can't change your own role,
  delete your own account, or remove the last remaining Administrator.
- Session cookies are signed with a key auto-generated into
  `pingmon.secret` on first run (kept separate from `pingmon.conf` so
  the config file's comments are never touched).

### Added — In-App Device Management
- Add, edit, and delete devices directly from each device group page
  (previously required hand-editing `.DAT` files). Writes are atomic
  (temp file + rename) and validated (no duplicate or malformed
  hostnames).
- Administrators can create new device groups (`.DAT` files) from the
  web UI (`/admin/groups/new`) instead of creating files by hand.

### Added — CSV Export
- "Export CSV" button on every device group page — exports that
  group's current device list (Hostname, IP Address, Location, Status,
  Latency, Last Seen).
- "Export All (CSV)" button on the dashboard — exports every configured
  device across all groups, with a Device Group column added.
- Both are open to anyone viewing the page, same as the tables
  themselves — no login required.

### Changed
- Device group files now use the **`.DAT`** extension instead of
  `.TXT` (`SWITCHES.DAT`, `UPS.DAT`, `LASERPRINTERS.DAT`,
  `ZEBRAPRINTERS.DAT`). Discovery, parsing, and logging were updated
  to match.
- `requirements.txt-` renamed back to `requirements.txt` — the
  trailing dash existed only to keep it out of the old `.TXT`
  device-group scan, which no longer applies now that groups use
  `.DAT`.
- Version bumped to **v3**, shown in the header tagline and in the
  footer of every page, including every form (login, change password,
  edit device, new group, user management, and the 403 page).

### Fixed
- Deleting a device (or renaming it via edit) now removes it from the
  dashboard and group tables immediately. Previously, the "current
  status" views were built entirely from ping history, which never
  expires — a removed device would keep showing up forever, frozen at
  its last known status, with no refresh able to clear it. The
  dashboard and group tables now cross-check against the current
  `.DAT` file on every load; historical data for removed devices is
  kept in the database but no longer surfaced in any table.

### Documentation
- README: new "Authentication & User Accounts" section (roles, default
  credentials, forced password change, where accounts are stored, how
  to reset to the default `admin` account), updated Web Interface
  route table (login required per route), updated folder layout and
  install instructions for the `.DAT` / `requirements.txt` changes.
