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

import csv
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
    """Never KeyErrors on a partial/older-schema state file -- a state file
    written by an earlier version of this script (missing "failed"), or one
    truncated by a non-atomic write, would otherwise crash here and take the
    whole done/attempts history down with it."""
    data = {}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "done": data.get("done") or [],
        "attempts": data.get("attempts") or {},
        "failed": data.get("failed") or {},
    }


def save_state(state: dict) -> None:
    # Atomic on the same filesystem: a crash mid-write can't leave a
    # truncated JSON file that load_state would then silently discard,
    # taking the whole done/attempts history with it.
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def log_result(name: str, output_name: str, status: str) -> None:
    is_new = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["timestamp", "input", "output", "status"])
        w.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S"), name, output_name,
            " ".join(status.split()),  # flatten embedded newlines (tracebacks)
        ])


def output_path(p: Path) -> Path:
    return p.with_name(f"{p.stem}{EN_SUFFIX}{p.suffix}")


def is_translated_output(p: Path) -> bool:
    return p.stem.lower().endswith(EN_SUFFIX)


def file_key(p: Path) -> str:
    """Path alone is not enough: replacing a file with a corrected scan
    under the same name would otherwise be skipped forever as 'already
    done', since done/attempts/failed are keyed on nothing but the relative
    path. Folding in size and mtime means a replaced file gets a new key
    and is retranslated once; old state-file entries just won't match the
    new key and degrade gracefully rather than erroring."""
    st = p.stat()
    return f"{p.relative_to(FOLDER)}:{st.st_size}:{int(st.st_mtime)}"


def progress(line: str) -> None:
    ts = time.strftime("%H:%M:%S")
    with open(PROGRESS_FILE, "a") as f:
        f.write(f"[{ts}] {line}\n")


def wait_until_stable(p: Path, checks: int = 2, interval: float = 1.0,
                       timeout: float = 120.0) -> bool:
    """True once p's size has been unchanged for `checks` consecutive polls.

    A Folder Action fires on "item added", which for a large file or a
    network/AirDrop copy can precede the last byte landing -- opening the
    file then yields a truncated PDF and burns a retry attempt on a file
    that was never actually broken."""
    deadline = time.monotonic() + timeout
    last, stable = -1, 0
    while time.monotonic() < deadline:
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            return False
        if size == last and size > 0:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0
        last = size
        time.sleep(interval)
    return False


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
    failed = state["failed"]

    def commit():
        state["done"] = sorted(done)
        state["attempts"] = attempts
        state["failed"] = failed
        save_state(state)

    # Case-insensitive: macOS's filesystem is case-insensitive by default,
    # but Path.rglob is not, so "*.pdf" alone silently skips a scanner- or
    # Windows-exported "Bericht.PDF".
    pdfs = sorted(p for p in FOLDER.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    pending = [
        p for p in pdfs
        if not is_translated_output(p)
        and file_key(p) not in done
        and file_key(p) not in failed
    ]

    if not pending:
        print("No new files to process.")
        return

    print(f"Found {len(pending)} new file(s) to process in {FOLDER}")

    # Import here (not at module load) so "no pending files" exits fast
    # without paying mlx-lm's/translate_pdf's import cost on every Folder
    # Action trigger -- and so an import failure (e.g. no serif font pair
    # found on this machine) is caught and logged instead of propagating
    # out of _main() past the lock's release with nothing recorded.
    try:
        from translate_pdf import DEFAULT_MODEL, process_pdf
    except Exception as e:
        print(f"FATAL: cannot import translate_pdf: {e}")
        progress(f"FATAL: cannot import translate_pdf: {e}")
        return

    for p in pending:
        key = file_key(p)
        out = output_path(p)

        if out.exists():
            print(f"  {key}: output already exists, marking done")
            done.add(key)
            attempts.pop(key, None)
            commit()
            continue

        if not wait_until_stable(p):
            print(f"  {key}: still being written, skipping this pass")
            continue  # no attempt burned; the next trigger picks it up

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
                # Not `done` -- that means "translated". A separate bucket
                # keeps it out of the pending queue while staying visible
                # and distinguishable from success; removing its entry from
                # the state file retries it.
                failed[key] = str(e)
                print(f"    giving up after {MAX_ATTEMPTS} attempts "
                      f"(remove '{key}' from {STATE_FILE.name}'s failed list to retry)")
            commit()
            continue

        out_key = str(out.relative_to(FOLDER))
        print(f"    done -> {out_key}")
        progress(f"done: {key} -> {out_key}")
        log_result(key, out_key, "ok")
        done.add(key)
        attempts.pop(key, None)
        commit()


if __name__ == "__main__":
    main()
