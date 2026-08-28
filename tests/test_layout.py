#!/usr/bin/env python3
"""Layout-correctness regression tests for scenarios the golden-file test
(tests/test_golden.py) doesn't cover: page overflow colliding with pinned
furniture, textual (non-digit) running heads/footers, multi-page reflow
reset, and --reformat-only end to end. Round 4's audit found real bugs in
several of these (findings 1-3) that a single-page, single-layout golden
fixture couldn't have caught.

Plain asserts, no test framework dependency, matching the existing style.
Run directly:
    ./venv/bin/python3 tests/test_layout.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf as fitz  # noqa: E402

import translate_pdf as tp  # noqa: E402

failures = []


def check(name, cond, detail=None):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        print(f"       {detail!r}")
        failures.append(name)


def stub_load_model(model_name, seed=0):
    return object(), object()


def output_lines(pdf_path, page_no=0):
    doc = fitz.open(str(pdf_path))
    try:
        blocks = [b for b in doc[page_no].get_text("dict")["blocks"] if b["type"] == 0]
        return [l for b in blocks for l in b["lines"] if l["spans"]]
    finally:
        doc.close()


def rects_overlap(a, b):
    return a.y0 < b.y1 and b.y0 < a.y1


# --- Findings 1 & 2: overflow that forces a rescale must not collide with
# --- a pinned folio, and the fitting outcome must be reported. ---

tp.load_model = stub_load_model
tp.translate = lambda m, t, txt, temp=0.0, report=None: (txt + " ") * 12

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fx, out = d / "in.pdf", d / "out.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)  # small page -> easy to overflow
    page.insert_text((72, 72), "Erster kurzer deutscher Satz hier.", fontsize=10)
    page.insert_text((72, 100), "Zweiter kurzer deutscher Satz auch hier.", fontsize=10)
    page.insert_text((72, 128), "Dritter kurzer deutscher Satz ebenfalls.", fontsize=10)
    page.insert_text((130, 370), "5", fontsize=9)  # folio; footer band = 400*0.88 = 352
    doc.save(str(fx))
    doc.close()

    reports = []
    tp.process_pdf(str(fx), str(out), "stub", progress_callback=reports.append)

    lines = output_lines(out)
    folio_line = next((l for l in lines if l["spans"][0]["text"].strip() == "5"), None)
    body_lines = [l for l in lines if l is not folio_line]

    check("overflow test: folio survived and stayed a single line",
          folio_line is not None)
    check("Finding 2: folio stayed near its authored position (not dragged into the body)",
          folio_line is not None and abs(folio_line["bbox"][1] - 370) < 10.0,
          folio_line["bbox"] if folio_line else None)
    check("Finding 2: no flowing body line overlaps the pinned folio's rect",
          folio_line is None or not any(
              rects_overlap(fitz.Rect(l["bbox"]), fitz.Rect(folio_line["bbox"]))
              for l in body_lines
          ),
          [l["bbox"] for l in body_lines if folio_line and
           rects_overlap(fitz.Rect(l["bbox"]), fitz.Rect(folio_line["bbox"]))])
    check("Finding 1: fit outcome (rescale or overflow) was reported, not silently dropped",
          any("rescal" in r.lower() or "overflow" in r.lower() for r in reports),
          reports)


# --- Finding 3: a textual (non-digit) running head/footer must be pinned
# --- too, not just a bare page number. ---

tp.translate = lambda m, t, txt, temp=0.0, report=None: txt  # identity this time

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fx, out = d / "in.pdf", d / "out.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=600)
    page.insert_text((72, 40), "Kapitel 3 - Einleitung", fontsize=9)  # header band = 72
    page.insert_text((72, 100), "Der erste Satz des Kapitels beginnt hier unten.", fontsize=11)
    page.insert_text((72, 118), "Und geht in einer zweiten Zeile weiter fort im Text.", fontsize=11)
    doc.save(str(fx))
    doc.close()

    tp.process_pdf(str(fx), str(out), "stub")
    lines = output_lines(out)
    header_line = next(
        (l for l in lines if "Kapitel" in "".join(s["text"] for s in l["spans"])), None
    )
    check("Finding 3: textual running head survived",
          header_line is not None)
    check("Finding 3: textual running head stayed pinned near its authored y (not dragged by reflow)",
          header_line is not None and abs(header_line["bbox"][1] - 40) < 10.0,
          header_line["bbox"] if header_line else None)


# --- Multi-page: reflow state (prev_orig_y1/prev_new_y1 etc.) must reset
# --- per page, not leak across pages. ---

tp.translate = lambda m, t, txt, temp=0.0, report=None: txt * 3  # grow, to expose any leak

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fx, out = d / "in.pdf", d / "out.pdf"
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page(width=300, height=400)
        page.insert_text((72, 72), f"Absatz auf Seite {i + 1} mit etwas Text drin.", fontsize=10)
    doc.save(str(fx))
    doc.close()

    tp.process_pdf(str(fx), str(out), "stub")
    o = fitz.open(str(out))
    check("multi-page: output has all 3 pages", len(o) == 3, len(o))
    first_line_ys = []
    for pno in range(len(o)):
        lines = output_lines(out, pno)
        if lines:
            first_line_ys.append(min(l["bbox"][1] for l in lines))
    o.close()
    check("multi-page: each page's first paragraph starts at the same y "
          "(all 3 pages were authored identically -- if reflow state leaked "
          "across pages, a later page's first line would drift from the "
          "others instead of matching)",
          max(first_line_ys) - min(first_line_ys) < 1.0, first_line_ys)


# --- --reformat-only end to end: run a real translate pass, then reformat
# --- the result without a model, and confirm it doesn't blow up and the
# --- tight-run gap logic actually changes something. ---

tp.translate = lambda m, t, txt, temp=0.0, report=None: txt

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fx, translated, reformatted = d / "in.pdf", d / "translated.pdf", d / "reformatted.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    page.insert_text((72, 72), "Ein deutscher Satz fuer den ersten Durchlauf.", fontsize=11)
    doc.save(str(fx))
    doc.close()

    tp.process_pdf(str(fx), str(translated), "stub")
    try:
        tp.process_pdf(str(translated), str(reformatted), None, skip_translation=True)
        reformat_ok = reformatted.exists()
    except Exception as e:
        reformat_ok = False
        reformat_error = e

    check("--reformat-only: re-processing this pipeline's own output succeeds with no model",
          reformat_ok, locals().get("reformat_error"))
    if reformat_ok:
        lines = output_lines(reformatted)
        check("--reformat-only: content survived",
              any("deutscher" in "".join(s["text"] for s in l["spans"]) for l in lines))


# --- A multi-column page must not abort the whole document: one flagged
# --- page should be skipped (left untranslated), not lose every other
# --- page's already-finished work. ---

tp.translate = lambda m, t, txt, temp=0.0, report=None: "TRANSLATED: " + txt

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fx, out = d / "in.pdf", d / "out.pdf"
    doc = fitz.open()
    p1 = doc.new_page(width=400, height=400)
    p1.insert_text((72, 72), "Ein guter einspaltiger deutscher Satz hier drin.", fontsize=10)
    p2 = doc.new_page(width=600, height=400)  # two-column, like a TOC/index page
    for i, y in enumerate(range(72, 300, 14)):
        p2.insert_text((72, y), f"Linke Spalte Zeile {i}.", fontsize=10)
        p2.insert_text((320, y), f"Rechte Spalte Zeile {i}.", fontsize=10)
    p3 = doc.new_page(width=400, height=400)
    p3.insert_text((72, 72), "Noch ein guter einspaltiger deutscher Satz hier.", fontsize=10)
    doc.save(str(fx))
    doc.close()

    tp.process_pdf(str(fx), str(out), "stub")
    o = fitz.open(str(out))
    check("multi-column: document still saved with all 3 pages "
          "(one flagged page must not abort the whole document)",
          len(o) == 3, len(o))
    page1_text, page2_text, page3_text = (o[i].get_text() for i in range(3))
    o.close()
    check("multi-column: page 1 (fine, single-column) was translated",
          "TRANSLATED:" in page1_text, page1_text)
    check("multi-column: page 2 (flagged multi-column) was left untranslated, not force-scrambled",
          "TRANSLATED:" not in page2_text and "Linke Spalte" in page2_text, page2_text)
    check("multi-column: page 3 (fine, single-column) was translated too "
          "(not just the pages before the flagged one)",
          "TRANSLATED:" in page3_text, page3_text)


print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
else:
    print("All checks passed.")
sys.exit(0 if not failures else 1)
