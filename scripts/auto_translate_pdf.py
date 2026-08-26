#!/usr/bin/env python3
"""
Watches ~/Translate and automatically translates any PDF dropped there from
German to English, producing "<name>_en.pdf" alongside the original (which is
left untouched), preserving layout via translate_pdf.py (TranslateGemma 4B,
run locally through mlx-lm).

This script is meant to be triggered automatically (e.g. via a macOS Folder
Action) every time a file is added to the folder, but it's also safe to just
run directly - it only touches files it hasn't successfully translated before.

Must be run with the project venv's Python (has mlx-lm, pymupdf installed):
    ~/auto-translate-pdf/venv/bin/python3 scripts/auto_translate_pdf.py
"""
from __future__ import annotations

import fcntl
import json
import sys
import time
from pathlib import Path

FOLDER = Path.home() / "Translate"
EN_SUFFIX = "_en"
MAX_ATTEMPTS = 3  # give up on a file after this many failed runs

STATE_FILE = FOLDER / ".auto_translate_state.json"
LOG_FILE = FOLDER / "translate_log.csv"
PROGRESS_FILE = FOLDER / ".translate_progress.log"

LOCK_FILE = Path.home() / "Library" / "Application Support" / "auto_translate_pdf.lock"

# translate_pdf.py and its DEFAULT_MODEL live at the repo root, one level up
# from this script (repo_root/scripts/auto_translate_pdf.py) -- not on the
# normal Python path.
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"done": [], "attempts": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def log_result(name: str, output_name: str, status: str) -> None:
    is_new = not LOG_FILE.exists()
    with open(LOG_FILE, "a") as f:
        if is_new:
            f.write("timestamp,input,output,status\n")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        n = name.replace('"', '""')
        o = output_name.replace('"', '""')
        s = status.replace('"', '""')
        f.write(f'{ts},"{n}","{o}","{s}"\n')


def output_path(p: Path) -> Path:
    return p.with_name(f"{p.stem}{EN_SUFFIX}{p.suffix}")


def is_translated_output(p: Path) -> bool:
    return p.stem.endswith(EN_SUFFIX)


def progress(line: str) -> None:
    ts = time.strftime("%H:%M:%S")
    with open(PROGRESS_FILE, "a") as f:
        f.write(f"[{ts}] {line}\n")


def main():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another instance (e.g. a duplicate Folder Action trigger, or a
        # still-running translation of an earlier file) is already going -
        # don't race it or double-load the model.
        print("Another auto_translate_pdf.py run is already in progress, exiting.")
        return

    try:
        _main()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _main():
    FOLDER.mkdir(parents=True, exist_ok=True)
    state = load_state()
    done = set(state["done"])
    attempts = state["attempts"]

    pdfs = sorted(FOLDER.rglob("*.pdf"))
    pending = [
        p for p in pdfs
        if not is_translated_output(p) and str(p.relative_to(FOLDER)) not in done
    ]

    if not pending:
        print("No new files to process.")
        return

    print(f"Found {len(pending)} new file(s) to process in {FOLDER}")

    # Import here (not at module load) so "no pending files" exits fast
    # without paying mlx-lm's import cost on every Folder Action trigger.
    from translate_pdf import DEFAULT_MODEL, process_pdf

    for p in pending:
        key = str(p.relative_to(FOLDER))
        out = output_path(p)

        if out.exists():
            print(f"  {key}: output already exists, marking done")
            done.add(key)
            attempts.pop(key, None)
            continue

        print(f"  translating: {key}")
        progress(f"start: {key}")
        try:
            process_pdf(str(p), str(out), DEFAULT_MODEL, progress_callback=progress)
        except Exception as e:
            attempts[key] = attempts.get(key, 0) + 1
            print(f"    ERROR: {e}")
            progress(f"  {key}: ERROR: {e}")
            log_result(key, "", f"error: {e}")
            if attempts[key] >= MAX_ATTEMPTS:
                done.add(key)
                print(f"    giving up after {MAX_ATTEMPTS} attempts")
            continue

        out_key = str(out.relative_to(FOLDER))
        print(f"    done -> {out_key}")
        progress(f"done: {key} -> {out_key}")
        log_result(key, out_key, "ok")
        done.add(key)
        attempts.pop(key, None)

    state["done"] = sorted(done)
    state["attempts"] = attempts
    save_state(state)


if __name__ == "__main__":
    main()
