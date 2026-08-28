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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf as fitz  # noqa: E402

import translate_pdf as tp  # noqa: E402
from fixtures import build_fixture  # noqa: E402

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
    # Lines 13pt apart (~1.3x the 10pt font -- an ordinary leading), not an
    # arbitrary wide spacing: since Round 5 Finding 2, split_page_into_
    # paragraphs now measures and preserves the source's own line-to-line
    # spacing as this paragraph's real leading, so an unrealistically wide
    # gap here would be honored as intentional and blow the translated
    # (12x-expanded) text's height far past what this test means to
    # exercise (a moderate, rescale-only overflow, not a true one).
    page.insert_text((72, 72), "Erster kurzer deutscher Satz hier.", fontsize=10)
    page.insert_text((72, 85), "Zweiter kurzer deutscher Satz auch hier.", fontsize=10)
    page.insert_text((72, 98), "Dritter kurzer deutscher Satz ebenfalls.", fontsize=10)
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


# --- LAYOUT_SUPPORT.md: a genuine multi-column page must now be
# --- translated correctly (region-aware split/reflow), not skipped and
# --- not zipped row-by-row into scrambled nonsense (the pre-region-layer
# --- behavior this whole feature replaces). Every page in a multi-page
# --- document, single- or multi-column, must come out translated. ---

translate_calls = []
tp.translate = lambda m, t, txt, temp=0.0, report=None: (
    translate_calls.append(txt) or ("TRANSLATED: " + txt)
)

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
    check("multi-column: document still saved with all 3 pages",
          len(o) == 3, len(o))
    page1_text, page2_text, page3_text = (
        " ".join(o[i].get_text().split()) for i in range(3)
    )
    o.close()
    check("multi-column: page 1 (fine, single-column) was translated",
          "TRANSLATED:" in page1_text, page1_text)
    check("multi-column: page 2 (genuinely two-column) is now translated too "
          "-- real column support, not the old skip-untranslated fallback",
          "TRANSLATED:" in page2_text and "Linke Spalte" in page2_text
          and "Rechte Spalte" in page2_text, page2_text)
    check("multi-column: the two columns were NOT zipped together row by "
          "row -- each column's lines were sent to translate() as their "
          "own coherent block, not interleaved with the other column's",
          any("Linke Spalte" in c and "Rechte Spalte" not in c for c in translate_calls)
          and any("Rechte Spalte" in c and "Linke Spalte" not in c for c in translate_calls),
          translate_calls,
    )
    check("multi-column: page 3 (fine, single-column) was translated too "
          "(not just the pages before/around the multi-column one)",
          "TRANSLATED:" in page3_text, page3_text)


# --- Round 5 Finding 1: a real PDF superscript footnote marker (ASCII
# --- digits, raised by the typesetter, ~0.83x body size -- the dead zone
# --- between the old size-only test and the Unicode-glyph test) must be
# --- recognized, not fused into the adjacent number. Built with
# --- insert_htmlbox's <sup> (not insert_text, which places literal glyphs
# --- with no superscript flag set at all). ---

translate_calls = []
tp.translate = lambda m, t, txt, temp=0.0, report=None: (translate_calls.append(txt) or txt)

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fx, out = d / "in.pdf", d / "out.pdf"
    doc = fitz.open()
    page = doc.new_page(width=600, height=300)
    page.insert_htmlbox(
        fitz.Rect(72, 80, 520, 200),
        "Horkheimer schrieb dies im Jahr 1938<sup>1</sup> in New York, kurz "
        "nach der Emigration.<br>Adorno widersprach ihm<sup>2</sup> in einem "
        "Brief aus Oxford.",
    )
    doc.save(str(fx))
    doc.close()

    tp.process_pdf(str(fx), str(out), "stub")

    joined = " ".join(translate_calls)
    check("Finding 1: the real superscript marker did NOT fuse into the "
          'adjacent year ("19381"); the year stayed "1938"',
          "19381" not in joined and "1938" in joined, translate_calls)
    check("Finding 1: both footnote markers survived as separate [N] tokens",
          "[1]" in joined and "[2]" in joined, translate_calls)


# --- Round 5 Finding 14: a /Rotate 90 page. AUDIT_FIXES.md's Round 4
# --- Finding 17 recorded this as spot-checked by hand and "an accident of
# --- PyMuPDF's coordinate handling, not something the code reasons about"
# --- -- pin the accident down before any future geometry change (leading,
# --- reformat idempotency, etc.) has a chance to silently break it. Works
# --- because get_text("dict")'s span bboxes and page.rect are BOTH already
# --- in the rotated/display coordinate space, not the underlying mediabox
# --- -- so extraction, redaction, and insert_htmlbox all agree without the
# --- pipeline needing to reason about the rotation transform itself.

tp.translate = lambda m, t, txt, temp=0.0, report=None: "TRANSLATED: " + txt

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fx, out = d / "in.pdf", d / "out.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=600)
    page.set_rotation(90)
    page.insert_text((72, 72), "Ein deutscher Satz auf einer gedrehten Seite hier drin.",
                      fontsize=10)
    doc.save(str(fx))
    doc.close()

    tp.process_pdf(str(fx), str(out), "stub")
    o = fitz.open(str(out))
    check("rotated page: /Rotate is preserved in the output",
          o[0].rotation == 90, o[0].rotation)
    check("rotated page: content was translated and survived",
          "TRANSLATED" in o[0].get_text() and "gedrehten" in o[0].get_text(),
          o[0].get_text())
    o.close()


# --- Round 5 Finding 2: the source's own line leading must be reproduced,
# --- not silently re-set at MuPDF's user-agent default of 1.2x. A
# --- paragraph set at 15pt leading on 11pt type (an entirely ordinary
# --- academic setting -- 1.36x) rendered at a fixed 13.2pt (1.2x) before
# --- this fix, off by up to +/-22% of the paragraph's height with *zero*
# --- text change (identity translation). ---

tp.translate = lambda m, t, txt, temp=0.0, report=None: txt  # identity -- isolates leading alone

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fx, out = d / "in.pdf", d / "out.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=700)
    source_leading = 15.0
    y = 72.0
    for i in range(6):
        x = 90 if i == 0 else 72  # first line indented, matching real body prose
        page.insert_text((x, y), f"Zeile Nummer {i} mit deutschem Text darin heute wirklich.",
                          fontsize=11)
        y += source_leading
    doc.save(str(fx))
    doc.close()

    tp.process_pdf(str(fx), str(out), "stub")
    lines = output_lines(out)
    ys = sorted(l["bbox"][1] for l in lines)
    deltas = [b - a for a, b in zip(ys, ys[1:])]
    avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
    check(f"Finding 2: output line spacing matches the source's {source_leading}pt "
          f"leading (not MuPDF's 1.2x-of-11pt = 13.2pt default)",
          abs(avg_delta - source_leading) < 0.5, avg_delta)


# --- Round 5 Finding 3: --reformat-only must be idempotent. Each pass used
# --- to add ~2pt of pad per paragraph to the page it re-extracts, so
# --- reformatting the same file repeatedly (the mode's own documented
# --- workflow: "a layout bug got fixed, re-run it on files produced
# --- before the fix") walked the body steadily down the page forever.
# ---
# --- Uses several ordinary multi-line body paragraphs (not the golden
# --- fixture's footnote entries, which take the fixed-3pt "tight run" gap
# --- branch in reformat mode and are drift-immune by construction either
# --- way) -- the compounding lives specifically in the general
# --- gap-preservation branch ordinary body prose takes.

tp.translate = lambda m, t, txt, temp=0.0, report=None: txt
archive, css = tp.font_setup()

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fx = d / "in.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=700)
    y = 72.0
    for i in range(6):
        html = (f'<p style="font-size:11pt; text-indent:18pt; text-align:justify;">'
                f"Absatz Nummer {i} mit etwas laengerem deutschem Text darin heute, "
                "der ueber zwei Zeilen laeuft mal wieder ganz bestimmt und weiter.</p>")
        rect = fitz.Rect(72, y, 328, y + 60)
        page.insert_htmlbox(rect, html, css=css, archive=archive)
        actual_lines = [
            l for b in page.get_text("dict")["blocks"] if b["type"] == 0
            for l in b["lines"] if l["bbox"][1] >= y - 1
        ]
        y = max(l["bbox"][3] for l in actual_lines) + 14  # real gap to the next paragraph
    doc.save(str(fx))
    doc.close()

    heights = []
    prev = fx
    for n in range(1, 4):
        out = d / f"reformatted_{n}.pdf"
        tp.process_pdf(str(prev), str(out), None, skip_translation=True)
        lines = output_lines(out)
        ys = [l["bbox"][1] for l in lines] + [l["bbox"][3] for l in lines]
        heights.append(max(ys) - min(ys))
        prev = out

    check("Finding 3: --reformat-only is idempotent -- body block height "
          "stays stable (within 1pt) across 3 successive reformat passes, "
          "not drifting further every time",
          max(heights) - min(heights) < 1.0, heights)


# --- Round 5 Finding 11: fit_and_insert must report when the real render
# --- disagrees with measure_height's prediction (scale_low=0 lets MuPDF
# --- shrink silently rather than fail) -- a deliberately undersized rect
# --- stands in for that disagreement. ---

reports = []
archive, css = tp.font_setup()
page = fitz.open().new_page(width=200, height=200)
tp.fit_and_insert(page, fitz.Rect(10, 10, 100, 20),
                   "Ein sehr langer deutscher Testsatz der garantiert nicht in diese "
                   "winzige Box passt.", 10.0, False, 0.0, None, reports.append)
check("Finding 11: a real measure/render mismatch (undersized rect, forced "
      "shrink) is reported, not silently absorbed",
      any("disagreed with the actual render" in r for r in reports), reports)

reports2 = []
page2 = fitz.open().new_page(width=400, height=400)
tp.fit_and_insert(page2, fitz.Rect(10, 10, 380, 100),
                   "Ein kurzer deutscher Satz.", 10.0, False, 0.0, None, reports2.append)
check("Finding 11: a normal, correctly-sized render reports nothing",
      reports2 == [], reports2)


# --- LAYOUT_SUPPORT.md Phase 5: tables are translated cell by cell, and
# --- numeric/symbolic cells are left untouched rather than sent to the
# --- model (a bare year gives it almost no context). ---

CORPUS = Path(__file__).resolve().parent / "corpus"

tp.load_model = lambda m, seed=0: (object(), object())
tp.translate = lambda m, t, txt, temp=0.0, report=None: "X-" + txt

with tempfile.TemporaryDirectory() as d:
    out = Path(d) / "table_out.pdf"
    tp.process_pdf(str(CORPUS / "table_ruled.pdf"), str(out), "stub")
    # insert_htmlbox can rewrite a real "-" into an invisible soft hyphen
    # (U+00AD) at its own internal wrap points -- same normalization
    # split_page_into_paragraphs already does for reflowed body text.
    text = " ".join(fitz.open(str(out))[0].get_text().replace("\xad", "-").split())
    check("table: text cells (header + data) were translated",
          all(f"X-{w}" in text for w in ["Year", "Edition", "Price", "First", "Second", "Third"]),
          text)
    check("table: numeric-only cells (years) were left untouched, not sent "
          "to the model as a near-empty translation request",
          all(f"X-{y}" not in text and y in text for y in ["1938", "1951", "1969"]),
          text)


# --- LAYOUT_SUPPORT.md Phase 2 acceptance: --reformat-only on a page with
# --- a figure must leave the figure alone (still present, nothing drawn
# --- over it) across repeated passes. ---

tp.translate = lambda m, t, txt, temp=0.0, report=None: txt

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    prev = CORPUS / "figure_inline.pdf"
    for n in range(1, 3):
        out = d / f"figure_r{n}.pdf"
        tp.process_pdf(str(prev), str(out), None, skip_translation=True)
        o = fitz.open(str(out))
        page = o[0]
        check(f"figure_inline.pdf reformat pass {n}: image still present",
              len(page.get_images()) == 1, page.get_images())
        img_rects = [fitz.Rect(r) for info in page.get_images(full=True)
                     for r in page.get_image_rects(info[0])]
        text_lines = [l for b in page.get_text("dict")["blocks"] if b["type"] == 0
                      for l in b["lines"]]
        overlap = [l["bbox"] for l in text_lines
                   for r in img_rects if fitz.Rect(l["bbox"]).intersects(r)]
        check(f"figure_inline.pdf reformat pass {n}: no text drawn over the image",
              not overlap, overlap)
        o.close()
        prev = out


def test_all_checks():
    """Synthetic pytest entry point. All the real checks above already ran
    as a side effect of importing this module (this file predates pytest
    and is designed to run standalone via `python tests/test_layout.py`,
    printing [PASS]/[FAIL] per check and continuing past a failure to
    report everything in one run, rather than stopping at the first
    `assert` the way a normal pytest test would) -- this just gives pytest
    something to collect, so `pytest tests/` works without rewriting the
    checks themselves (Round 5 Finding 13)."""
    assert not failures, f"{len(failures)} check(s) failed: {', '.join(failures)}"


if __name__ == "__main__":
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
    else:
        print("All checks passed.")
    sys.exit(0 if not failures else 1)
