#!/usr/bin/env python3
"""
prune_db.py — PingMon Database Pruning Utility
───────────────────────────────────────────────
Safely removes old ping history from pingmon.db and reclaims disk space.

Usage
-----
  python3 prune_db.py                  # interactive — asks for retention days
  python3 prune_db.py --days 90        # keep last 90 days, still asks to confirm
  python3 prune_db.py --days 90 --yes  # fully non-interactive (scripted / cron use)
  python3 prune_db.py --days 90 --no-vacuum   # skip VACUUM (faster, no compaction)
  python3 prune_db.py --backup-only    # just take a backup, delete nothing

Notes
-----
  • PingMon does NOT need to be stopped. The database runs in WAL mode which
    allows safe concurrent access during pruning.
  • A timestamped backup is ALWAYS created before any rows are deleted.
  • The script exits with code 0 on success, non-zero on any error.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── ANSI colours (same palette as PingMon's logger) ──────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        m = ctypes.c_ulong()
        if k.GetConsoleMode(h, ctypes.byref(m)):
            k.SetConsoleMode(h, m.value | 0x0004)
    except Exception:
        pass

CYAN      = "\x1b[36;20m"
YELLOW    = "\x1b[33;20m"
GREEN     = "\x1b[32;20m"
RED       = "\x1b[31;20m"
BOLD      = "\x1b[1m"
RESET     = "\x1b[0m"

def info(msg):    print(f"{CYAN}[INFO]{RESET}  {msg}")
def warn(msg):    print(f"{YELLOW}[WARN]{RESET}  {msg}")
def ok(msg):      print(f"{GREEN}[OK]{RESET}    {msg}")
def error(msg):   print(f"{RED}[ERROR]{RESET} {msg}")
def header(msg):  print(f"\n{BOLD}{msg}{RESET}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def fmt_rows(n: int) -> str:
    return f"{n:,}"

def db_file_size(path: Path) -> int:
    """Return the combined size of the DB file plus any WAL/SHM sidecar files."""
    total = path.stat().st_size if path.exists() else 0
    for ext in ("-wal", "-shm"):
        side = path.with_suffix(path.suffix + ext)
        if side.exists():
            total += side.stat().st_size
    return total

def make_backup(db_path: Path) -> Path:
    """
    Use SQLite's online backup API (safe with active writers) to create a
    timestamped copy of the database.  Returns the backup path.
    """
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_dir = db_path.parent / "backups"
    bak_dir.mkdir(exist_ok=True)
    bak_path = bak_dir / f"{db_path.stem}_backup_{ts}{db_path.suffix}"

    info(f"Creating backup → {bak_path.relative_to(db_path.parent.parent) if bak_path.is_relative_to(db_path.parent.parent) else bak_path}")

    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(bak_path))
    try:
        src.backup(dst)          # online backup — safe even while PingMon runs
    finally:
        dst.close()
        src.close()

    ok(f"Backup complete  ({fmt_bytes(bak_path.stat().st_size)})")
    return bak_path

def count_rows(conn: sqlite3.Connection, cutoff: str) -> tuple[int, int]:
    """Return (total_rows, rows_older_than_cutoff)."""
    total  = conn.execute("SELECT COUNT(*) FROM ping_results").fetchone()[0]
    old    = conn.execute(
        "SELECT COUNT(*) FROM ping_results WHERE pinged_at < ?", (cutoff,)
    ).fetchone()[0]
    return total, old

def date_range(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute(
        "SELECT MIN(pinged_at), MAX(pinged_at) FROM ping_results"
    ).fetchone()
    return row[0] or "—", row[1] or "—"

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Prune old records from the PingMon SQLite database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Retain records from the last N days (default: ask interactively).",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Path to pingmon.db  (default: same folder as this script).",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt — useful for scheduled/automated runs.",
    )
    parser.add_argument(
        "--no-vacuum", action="store_true",
        help="Skip the VACUUM step (faster but does not reclaim disk space).",
    )
    parser.add_argument(
        "--backup-only", action="store_true",
        help="Create a backup and exit without deleting any records.",
    )
    args = parser.parse_args()

    # ── Locate the database ───────────────────────────────────────────────
    if args.db:
        db_path = Path(args.db).resolve()
    else:
        db_path = Path(__file__).parent.resolve() / "pingmon.db"

    header("═══  PingMon Database Pruning Utility  ═══")
    print()

    if not db_path.exists():
        error(f"Database not found: {db_path}")
        error("Make sure PingMon has run at least once, or pass --db <path>.")
        sys.exit(1)

    # Quick sanity check — is this actually a SQLite file?
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("SELECT COUNT(*) FROM ping_results")
    except sqlite3.DatabaseError as e:
        error(f"Cannot open database: {e}")
        sys.exit(1)

    # ── Show current state ────────────────────────────────────────────────
    size_before = db_file_size(db_path)
    oldest, newest = date_range(conn)
    total, _ = count_rows(conn, "9999-99-99")   # cutoff far future → 0 old rows

    info(f"Database file : {db_path}")
    info(f"Current size  : {fmt_bytes(size_before)}")
    info(f"Total rows    : {fmt_rows(total)}")
    info(f"Oldest record : {oldest}")
    info(f"Newest record : {newest}")
    print()

    if args.backup_only:
        make_backup(db_path)
        conn.close()
        print()
        ok("Backup-only run complete. No records were deleted.")
        sys.exit(0)

    # ── Determine retention period ────────────────────────────────────────
    days = args.days
    if days is None:
        print(f"{BOLD}How many days of history do you want to keep?{RESET}")
        print("  Common choices: 30 (one month)  |  90 (three months)  |  365 (one year)")
        while True:
            try:
                raw = input("  Keep last [days]: ").strip()
                days = int(raw)
                if days < 1:
                    raise ValueError
                break
            except ValueError:
                warn("Please enter a whole number greater than 0.")

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    _, old_count = count_rows(conn, cutoff)
    keep_count   = total - old_count

    print()
    info(f"Retention policy : keep last {days} day(s)  (records before {cutoff})")
    info(f"Rows to DELETE   : {fmt_rows(old_count)}  ({old_count / total * 100:.1f}% of total)" if total else "Rows to DELETE   : 0")
    info(f"Rows to KEEP     : {fmt_rows(keep_count)}")
    print()

    if old_count == 0:
        ok("Nothing to prune — all records are within the retention window.")
        conn.close()
        sys.exit(0)

    # ── Confirm ───────────────────────────────────────────────────────────
    if not args.yes:
        print(f"{YELLOW}This will permanently delete {fmt_rows(old_count)} rows.{RESET}")
        print("A backup will be created first.")
        answer = input("  Proceed? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            warn("Aborted — no changes made.")
            conn.close()
            sys.exit(0)

    print()

    # ── Backup ────────────────────────────────────────────────────────────
    try:
        bak_path = make_backup(db_path)
    except Exception as e:
        error(f"Backup failed: {e}")
        error("Aborting — no records were deleted.")
        conn.close()
        sys.exit(1)

    print()

    # ── Prune ─────────────────────────────────────────────────────────────
    info(f"Deleting {fmt_rows(old_count)} rows older than {cutoff} …")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        deleted = conn.execute(
            "DELETE FROM ping_results WHERE pinged_at < ?", (cutoff,)
        ).rowcount
        conn.commit()
        ok(f"Deleted {fmt_rows(deleted)} rows.")
    except sqlite3.Error as e:
        error(f"DELETE failed: {e}")
        error(f"Your original data is safe in the backup: {bak_path}")
        conn.close()
        sys.exit(1)

    # ── VACUUM ────────────────────────────────────────────────────────────
    if not args.no_vacuum:
        info("Running VACUUM to reclaim disk space …")
        try:
            conn.execute("VACUUM")
            ok("VACUUM complete.")
        except sqlite3.Error as e:
            warn(f"VACUUM failed (non-fatal): {e}")
            warn("Records were deleted successfully; disk space may not be fully reclaimed.")
    else:
        warn("Skipping VACUUM (--no-vacuum). Disk space not yet reclaimed.")

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────
    size_after = db_file_size(db_path)
    saved      = size_before - size_after

    print()
    header("═══  Summary  ═══")
    print()
    ok(f"Rows deleted    : {fmt_rows(deleted)}")
    ok(f"Rows remaining  : {fmt_rows(keep_count)}")
    ok(f"Size before     : {fmt_bytes(size_before)}")
    ok(f"Size after      : {fmt_bytes(size_after)}")
    ok(f"Space reclaimed : {fmt_bytes(max(saved, 0))}")
    ok(f"Backup location : {bak_path}")
    print()

if __name__ == "__main__":
    main()
