"""
Digi-Exam Backup Script
=======================
Creates timestamped backups of the project's critical data:
  - SQLite databases (digiexam.db, edge_terminal_logs.db)
  - Uploaded files (uploads/)
  - Configuration / sample data (sample_exam.json, sample_roster.csv)

Usage:
    python backup.py              # backs up to ./backups/<timestamp>/
    python backup.py -o D:/safe   # backs up to D:/safe/<timestamp>/
    python backup.py --list       # list existing backups
    python backup.py --restore <folder>  # restore from a specific backup
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ── Project root (same directory as this script) ────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent

# ── Items to back up ────────────────────────────────────────────────────
BACKUP_ITEMS = [
    # Databases
    PROJECT_ROOT / "digiexam.db",
    PROJECT_ROOT / "edge_terminal" / "edge_terminal_logs.db",
    # Uploads directory
    PROJECT_ROOT / "uploads",
    # Config / sample data
    PROJECT_ROOT / "sample_exam.json",
    PROJECT_ROOT / "sample_roster.csv",
    # Mock emails (if any)
    PROJECT_ROOT / "mock_sent_emails",
]

DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups"


def safe_copy_sqlite(src: Path, dst: Path):
    """Use SQLite's online-backup API so the copy is always consistent,
    even if the application is running."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(str(src))
    dest_conn = sqlite3.connect(str(dst))
    with dest_conn:
        source_conn.backup(dest_conn)
    source_conn.close()
    dest_conn.close()


def create_backup(output_root: Path):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = output_root / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  Digi-Exam Backup")
    print(f"  {'=' * 40}")
    print(f"  Destination: {backup_dir}\n")

    backed_up = 0

    for item in BACKUP_ITEMS:
        if not item.exists():
            print(f"  [SKIP]  {item.relative_to(PROJECT_ROOT)}  (not found)")
            continue

        rel = item.relative_to(PROJECT_ROOT)
        dest = backup_dir / rel

        if item.is_file():
            if item.suffix == ".db":
                # Use safe SQLite backup
                safe_copy_sqlite(item, dest)
                size_kb = dest.stat().st_size / 1024
                print(f"  [DB]    {rel}  ({size_kb:.1f} KB)")
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                size_kb = dest.stat().st_size / 1024
                print(f"  [FILE]  {rel}  ({size_kb:.1f} KB)")
            backed_up += 1

        elif item.is_dir():
            files = list(item.rglob("*"))
            file_count = sum(1 for f in files if f.is_file())
            if file_count == 0:
                print(f"  [SKIP]  {rel}/  (empty directory)")
                continue
            shutil.copytree(item, dest, dirs_exist_ok=True)
            total_size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
            print(f"  [DIR]   {rel}/  ({file_count} files, {total_size / 1024:.1f} KB)")
            backed_up += 1

    print(f"\n  {'=' * 40}")
    print(f"  Done! {backed_up} item(s) backed up to:")
    print(f"  {backup_dir}\n")
    return backup_dir


def list_backups(output_root: Path):
    if not output_root.exists():
        print("\n  No backups found.\n")
        return

    folders = sorted(
        [d for d in output_root.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    if not folders:
        print("\n  No backups found.\n")
        return

    print(f"\n  Existing Backups ({output_root})")
    print(f"  {'=' * 50}")
    for folder in folders:
        files = list(folder.rglob("*"))
        file_count = sum(1 for f in files if f.is_file())
        total_size = sum(f.stat().st_size for f in files if f.is_file())
        print(f"  {folder.name}  ({file_count} files, {total_size / 1024:.1f} KB)")
    print()


def restore_backup(output_root: Path, folder_name: str):
    backup_dir = output_root / folder_name
    if not backup_dir.exists():
        print(f"\n  Error: backup '{folder_name}' not found in {output_root}\n")
        sys.exit(1)

    print(f"\n  Restoring from: {backup_dir}")
    print(f"  {'=' * 40}")
    print(f"  WARNING: This will OVERWRITE current project data!")
    confirm = input("  Type 'yes' to confirm: ").strip().lower()
    if confirm != "yes":
        print("  Restore cancelled.\n")
        return

    restored = 0
    for item in backup_dir.iterdir():
        dest = PROJECT_ROOT / item.relative_to(backup_dir)
        if item.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            print(f"  [OK]  {item.relative_to(backup_dir)}")
            restored += 1
        elif item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
            print(f"  [OK]  {item.relative_to(backup_dir)}/")
            restored += 1

    print(f"\n  Restored {restored} item(s).\n")


def main():
    parser = argparse.ArgumentParser(description="Digi-Exam Backup Utility")
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help=f"Root directory for backups (default: {DEFAULT_BACKUP_DIR})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List existing backups",
    )
    parser.add_argument(
        "--restore",
        type=str,
        metavar="FOLDER",
        help="Restore from a specific backup folder name (e.g. 20260330_101500)",
    )
    args = parser.parse_args()

    if args.list:
        list_backups(args.output)
    elif args.restore:
        restore_backup(args.output, args.restore)
    else:
        create_backup(args.output)


if __name__ == "__main__":
    main()
