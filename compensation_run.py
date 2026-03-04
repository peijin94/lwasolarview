#!/usr/bin/env python3
"""
Compensation run for failed days listed in failed_days.txt.

Reads comma-separated dates (e.g. 2023-10-20) and runs:
  python generate_all_spectra.py --use_xhand_book --oneday --onedaypath <path>
for each day, where path = /nas7a/beam/beam-data/YYYYMM/beamYYYYMMDD.

Usage:
  python compensation_run.py [--list] [--dry-run]
  python compensation_run.py --file other_failed.txt
"""

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_LIST = SCRIPT_DIR / "failed_days.txt"
DATA_ROOT = "/nas7a/beam/beam-data"


def date_to_path(date_str):
    """Convert 'YYYY-MM-DD' to /nas7a/beam/beam-data/YYYYMM/beamYYYYMMDD."""
    date_str = date_str.strip()
    if not date_str:
        return None
    # YYYY-MM-DD -> YYYYMM, YYYYMMDD
    parts = date_str.split("-")
    if len(parts) != 3:
        return None
    y, m, d = parts[0], parts[1], parts[2]
    yyyymm = y + m
    yyyymmdd = y + m + d
    return f"{DATA_ROOT}/{yyyymm}/beam{yyyymmdd}"


def main():
    ap = argparse.ArgumentParser(description="Compensation run for failed days")
    ap.add_argument(
        "--file", "-f",
        default=str(DEFAULT_LIST),
        help="Path to file listing failed days (default: failed_days.txt)",
    )
    ap.add_argument(
        "--list", "-l",
        action="store_true",
        help="Only list the paths that would be run, then exit",
    )
    ap.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Print the command for each day but do not run",
    )
    ap.add_argument(
        "--mode",
        default="original",
        choices=["original", "open", "background"],
        help="Processing mode (default: original)",
    )
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    text = path.read_text()
    # Split on comma and newline, then clean
    raw_dates = []
    for line in text.splitlines():
        raw_dates.extend(s.strip() for s in line.split(",") if s.strip())
    days = []
    for d in raw_dates:
        p = date_to_path(d)
        if p is None:
            print(f"Skip invalid date: {d!r}", file=sys.stderr)
            continue
        days.append((d, p))

    if not days:
        print("No valid dates found.", file=sys.stderr)
        sys.exit(1)

    if args.list:
        for _, p in days:
            print(p)
        return

    generate_script = SCRIPT_DIR / "generate_all_spectra.py"
    cmd_base = [
        sys.executable,
        str(generate_script),
        "--use_xhand_book",
        "--mode", args.mode,
        "--oneday",
    ]

    for i, (date_str, onedaypath) in enumerate(days, 1):
        cmd = cmd_base + ["--onedaypath", onedaypath]
        print(f"[{i}/{len(days)}] {date_str} -> {onedaypath}")
        if args.dry_run:
            print("  ", " ".join(cmd))
            continue
        ret = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
        if ret.returncode != 0:
            print(f"  FAILED (exit {ret.returncode})", file=sys.stderr)
            # Continue with next day; comment out the next line to stop on first failure
            # sys.exit(ret.returncode)

    print("Done.")


if __name__ == "__main__":
    main()
