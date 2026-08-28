#!/usr/bin/env python3
"""
Watches ~/Translate and automatically translates any PDF dropped there from
German to English, producing "<name>_en<ext>" alongside the original (which is
left untouched, extension case preserved -- a "Bericht.PDF" input produces
"Bericht_en.PDF", not "Bericht_en.pdf"), preserving layout via
translate_pdf.py (TranslateGemma 4B, run locally through mlx-lm).

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
import os
import subprocess
import sys
import time
from pathlib import Path

FOLDER = Path.home() / "Translate"
EN_SUFFIX = "_en"
MAX_ATTEMPTS = 3  # give up on a file after this many failed runs

# A macOS Folder Action fires on *item added* to FOLDER -- so any of this
# script's own bookkeeping files that live inside FOLDER re-trigger the very
# Folder Action that runs this script. Each spurious trigger is cheap (it
# hits the flock and either finds nothing pending or re-processes an
# already-done file for free), but it's an avoidable feedback loop, and it
# clutters the folder the user is actually looking at. Only the human-
# facing translate_log.csv stays in FOLDER: it's append-only, so it doesn't
# re-trigger after its own creation, and it's the one file a user watching
# this folder would actually want to find there.
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "auto_translate_pdf"
STATE_FILE = APP_SUPPORT_DIR / "state.json"
PROGRESS_FILE = APP_SUPPORT_DIR / "progress.log"
RETRY_MARKER = APP_SUPPORT_DIR / "retry_pending"
LOG_FILE = FOLDER / "translate_log.csv"
RETRY_DELAY = 150.0  # seconds; comfortably longer than wait_until_stable's 120s timeout
STALE_RETRY_MARKER_AGE = RETRY_DELAY * 3  # older than this -> the marker is presumed stranded

LOCK_FILE = APP_SUPPORT_DIR / "auto_translate_pdf.lock"

PROGRESS_LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate (truncate) past this size

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
    # p.suffix preserves the original extension's case verbatim (".PDF" in,
    # ".PDF" out) -- only the inserted "_en" is always lowercase, so a
    # scanner export like "Bericht.PDF" becomes "Bericht_en.PDF", not
    # "Bericht_en.pdf".
    return p.with_name(f"{p.stem}{EN_SUFFIX}{p.suffix}")


def is_translated_output(p: Path) -> bool:
    return p.stem.lower().endswith(EN_SUFFIX)


def file_key(p: Path) -> str | None:
    """Path alone is not enough: replacing a file with a corrected scan
    under the same name would otherwise be skipped forever as 'already
    done', since done/attempts/failed are keyed on nothing but the relative
    path. Folding in size and mtime means a replaced file gets a new key
    and is retranslated once; old state-file entries just won't match the
    new key and degrade gracefully rather than erroring.

    Returns None if `p` no longer exists -- a normal thing to happen to a
    file sitting in a watched drop folder between the initial rglob() and
    this being called (the user moves it, an editor does an atomic
    save-and-replace, etc.). An uncaught FileNotFoundError here used to
    take down the whole run, including every file queued behind it."""
    try:
        st = p.stat()
    except OSError:
        return None
    return f"{p.relative_to(FOLDER)}:{st.st_size}:{int(st.st_mtime)}"


def rotate_progress_log_if_needed() -> None:
    """`.translate_progress.log` grows without bound -- every paragraph of
    every document ever translated, appended forever, one open() per line.
    Truncate (not delete, so a tail -f watching it doesn't need to reopen)
    once it passes PROGRESS_LOG_MAX_BYTES."""
    try:
        if PROGRESS_FILE.stat().st_size > PROGRESS_LOG_MAX_BYTES:
            PROGRESS_FILE.write_text("")
    except FileNotFoundError:
        pass


def progress(line: str) -> None:
    ts = time.strftime("%H:%M:%S")
    with open(PROGRESS_FILE, "a") as f:
        f.write(f"[{ts}] {line}\n")


def wait_until_stable(p: Path, checks: int = 2, interval: float = 1.0,
                       timeout: float = 120.0, zero_byte_timeout: float = 5.0) -> bool:
    """True once p's size has been unchanged for `checks` consecutive polls.

    A Folder Action fires on "item added", which for a large file or a
    network/AirDrop copy can precede the last byte landing -- opening the
    file then yields a truncated PDF and burns a retry attempt on a file
    that was never actually broken."""
    try:
        st = p.stat()
    except FileNotFoundError:
        return False

    # Fast path: a file whose last write already happened comfortably in
    # the past (e.g. one that finished copying seconds before this trigger
    # fired, or a long-stationary file re-checked on a later run) doesn't
    # need to pay the full `checks * interval` polling cost just to prove
    # what its mtime already shows -- nothing has touched it recently.
    if st.st_size > 0 and time.time() - st.st_mtime > interval * checks:
        return True

    deadline = time.monotonic() + timeout
    last, stable, zero_since = st.st_size, 0, (time.monotonic() if st.st_size == 0 else None)
    while time.monotonic() < deadline:
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            return False
        if size == 0:
            # A 0-byte file that never grows (a placeholder created before
            # writing starts, or a genuinely empty/broken drop) will never
            # satisfy the stability check below -- without this, every
            # trigger burns the *entire* 120s timeout on it for nothing.
            # Give it a much shorter window to start growing before bailing.
            zero_since = zero_since or time.monotonic()
            if time.monotonic() - zero_since >= zero_byte_timeout:
                return False
        else:
            zero_since = None
        if size == last and size > 0:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0
        last = size
        time.sleep(interval)
    return False


def cleanup_stale_part_files() -> None:
    """Remove any leftover "*.part" temp file at the start of a run.

    translate_pdf.py saves to "<output>.part" and renames it to the final
    name only on success, specifically so a mid-run failure never leaves a
    corrupt/partial file that out.exists() would mistake for a completed
    translation. But a *hard* kill (force-quit, `kill -9`, a crashed
    process) skips that rename entirely and leaves the ".part" file
    sitting in the folder forever as harmless-but-permanent clutter. Since
    we're only ever called while holding LOCK_FILE, any ".part" file found
    here cannot belong to a still-running translation -- it's necessarily
    stale."""
    for part in FOLDER.rglob("*.part"):
        try:
            part.unlink()
            print(f"  removed stale partial file: {part.relative_to(FOLDER)}")
        except OSError:
            pass


def schedule_retry() -> None:
    """Spawn a detached process that re-invokes this script after a delay.

    A Folder Action fires only on "item added" -- if wait_until_stable()
    times out on a copy slower than its own 120s window, nothing will ever
    trigger this script again for that file until some *other* file is
    dropped in the folder. Left alone, a single slow copy can strand
    itself indefinitely. RETRY_MARKER de-dupes: only one retry is ever
    outstanding at a time, so a busy folder doesn't spawn a growing pile of
    them (the marker is normally cleared once a run completes with nothing
    left waiting on stability).

    That "normally" matters: the marker can also be stranded -- _main()
    raising anywhere after it's written (a crash in commit()/save_state()/
    log_result(), none of which are wrapped in a try), or the detached
    child simply never running (the machine sleeps, reboots, gets killed).
    Once stranded, this function returned early forever with no marker
    ever there to expire it -- exactly the "a slow copy is never retried"
    failure this whole mechanism exists to prevent, just moved one level
    up. The marker's own age (it stores its write time) is checked before
    treating it as still-live."""
    if RETRY_MARKER.exists():
        try:
            age = time.time() - float(RETRY_MARKER.read_text().strip())
        except (ValueError, OSError):
            age = STALE_RETRY_MARKER_AGE + 1  # unreadable -> treat as stale
        if age < STALE_RETRY_MARKER_AGE:
            return
        # Stale: fall through and schedule a fresh retry.
    RETRY_MARKER.write_text(str(time.time()))
    script = str(Path(__file__).resolve())
    # No shell involved, so no quoting to get wrong: `!r` (Python repr, not
    # POSIX quoting) inside a "/bin/sh -c ..." string used to build a
    # broken -- and in principle injectable -- command line for any repo
    # path containing a shell-special character (a single quote, e.g.
    # "/Users/me/Anna's Docs/auto-translate-pdf"). Sleeping in the child
    # and calling runpy directly needs no shell at all.
    subprocess.Popen(
        [sys.executable, "-c",
         "import runpy, sys, time; time.sleep(float(sys.argv[1])); "
         "sys.argv = [sys.argv[2]]; runpy.run_path(sys.argv[0], run_name='__main__')",
         str(RETRY_DELAY), script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"  scheduled a retry in {RETRY_DELAY:.0f}s for the still-copying file(s)")


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
    rotate_progress_log_if_needed()
    cleanup_stale_part_files()
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
    pending = []
    debug = bool(os.environ.get("AUTO_TRANSLATE_DEBUG"))
    for p in pdfs:
        if is_translated_output(p):
            if debug:
                # A source PDF genuinely named to end in "_en" (not a prior
                # translation output) is indistinguishable from one and
                # gets skipped here with no message by default -- opt into
                # AUTO_TRANSLATE_DEBUG=1 to see which files this affects.
                print(f"  DEBUG: skipping {p.relative_to(FOLDER)} "
                      f"(name ends in {EN_SUFFIX!r}, assumed already translated)")
            continue
        key = file_key(p)
        if key is None:
            # Disappeared (moved/deleted) between the rglob() above and
            # here -- a normal thing to do to a file in a watched drop
            # folder. Skip it instead of crashing; the next trigger will
            # either not see it at all, or see it again with a fresh key.
            continue
        if key in done or key in failed:
            continue
        pending.append((p, key))

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
        from translate_pdf import DEFAULT_MODEL, UnsupportedInputError, process_pdf
    except Exception as e:
        print(f"FATAL: cannot import translate_pdf: {e}")
        progress(f"FATAL: cannot import translate_pdf: {e}")
        # Every other failure path in this loop calls log_result() so it
        # shows up in translate_log.csv -- this one didn't, so a broken
        # environment (e.g. no serif font pair on this machine) silently
        # skipped the one record a user checking the log would look for.
        log_result("", "", f"fatal: cannot import translate_pdf: {e}")
        return

    still_copying = False
    for p, key in pending:
        out = output_path(p)

        if out.exists():
            print(f"  {key}: output already exists, marking done")
            done.add(key)
            attempts.pop(key, None)
            commit()
            continue

        if not wait_until_stable(p):
            print(f"  {key}: still being written, skipping this pass")
            still_copying = True
            continue  # no attempt burned; the next trigger picks it up

        print(f"  translating: {key}")
        progress(f"start: {key}")
        try:
            process_pdf(str(p), str(out), DEFAULT_MODEL, progress_callback=progress)
        except UnsupportedInputError as e:
            # An encrypted PDF, an image-only scan, or an empty document
            # will fail identically on every attempt -- go straight to
            # `failed` instead of burning MAX_ATTEMPTS retries that can't
            # possibly succeed, so the real problem (needs OCR, needs a
            # password) surfaces immediately instead of after 3 wasted runs.
            print(f"    UNSUPPORTED: {e}")
            progress(f"  {key}: UNSUPPORTED: {e}")
            log_result(key, "", f"unsupported: {e}")
            failed[key] = str(e)
            attempts.pop(key, None)
            commit()
            continue
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

    if still_copying:
        schedule_retry()
    else:
        # Nothing left waiting on stability -- clear the marker so a future
        # slow copy is free to schedule its own retry rather than finding
        # a stale marker from a batch that has since finished.
        RETRY_MARKER.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
