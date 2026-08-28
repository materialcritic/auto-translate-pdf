#!/usr/bin/env python3
"""Builds tests/corpus/*.pdf -- the layout-support test corpus (LAYOUT_SUPPORT.md
section 2.1). Generated programmatically with PyMuPDF rather than from LaTeX/
Word (no such toolchain available here); layout-phase work is testable via
--check/--reformat-only either way, so the corpus doesn't need to be German
prose, matching LAYOUT_SUPPORT.md's own note that phases 0-4 work is layout-
only. Run directly to (re)generate the corpus:
    ./venv/bin/python3 tests/build_corpus.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf as fitz  # noqa: E402

from translate_pdf import font_setup  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus"


def _para(page, rect, html, css, archive):
    page.insert_htmlbox(rect, html, css=css, archive=archive)


def build_single_column(path):
    """A page the current pipeline already handles well -- the regression
    guard every later phase must not disturb."""
    archive, css = font_setup()
    doc = fitz.open()
    page = doc.new_page(width=450, height=650)
    _para(page, fitz.Rect(72, 72, 378, 110),
          '<p style="font-size:14pt;">Introduction</p>', css, archive)
    _para(page, fitz.Rect(72, 120, 378, 400),
          '<p style="font-size:11pt; text-indent:18pt; text-align:justify;">'
          "The author describes the situation in considerable detail, "
          "elaborating on several points that recur throughout the "
          "remainder of the chapter and setting up the argument that "
          "follows in the next section of this ordinary single-column "
          "academic document.</p>", css, archive)
    _para(page, fitz.Rect(72, 410, 378, 600),
          '<p style="font-size:11pt; text-indent:18pt; text-align:justify;">'
          "A second paragraph continues the discussion, referring back to "
          "the first while introducing a new consideration that will be "
          "developed further in later chapters of this document.</p>",
          css, archive)
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def build_two_column(path):
    """Plain two-column body, no spanning heading."""
    archive, css = font_setup()
    doc = fitz.open()
    page = doc.new_page(width=600, height=500)
    left_text = (
        "Column one begins here with a reasonably long run of prose that "
        "continues for several lines so the column-detection code has "
        "enough lines to work with when looking for a real gutter between "
        "the two halves of this page, which is set in two columns "
        "throughout, left and right, side by side across the sheet."
    )
    right_text = (
        "Column two begins here with an entirely different sentence, "
        "continuing the discussion from a different angle for several "
        "lines so that this half of the page also has enough lines for "
        "the gutter-detection code to find, sitting beside column one "
        "across a clear vertical gap that should read as two columns."
    )
    _para(page, fitz.Rect(50, 60, 290, 440),
          f'<p style="font-size:10pt; text-align:justify;">{left_text}</p>',
          css, archive)
    _para(page, fitz.Rect(320, 60, 560, 440),
          f'<p style="font-size:10pt; text-align:justify;">{right_text}</p>',
          css, archive)
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def build_two_column_heading(path):
    """Two columns with a full-width title/heading spanning both."""
    archive, css = font_setup()
    doc = fitz.open()
    page = doc.new_page(width=600, height=500)
    _para(page, fitz.Rect(50, 40, 560, 70),
          # Long enough that its rendered glyph-bbox width itself clears
          # SPAN_LINE_FRAC (60% of the area width) -- centering only
          # affects *position*, not the tight bbox get_text() reports, so
          # a short centered title would (and initially did) fail to
          # register as a spanning line at all.
          '<p style="font-size:16pt; text-align:center;">'
          'A Two-Column Article With a Sufficiently Long Title</p>',
          css, archive)
    left_text = (
        "Column one begins below the spanning heading with a reasonably "
        "long run of prose that continues for several lines so the "
        "column-detection code has enough lines to work with in this band."
    )
    right_text = (
        "Column two begins below the same spanning heading with different "
        "prose, continuing for several lines so this half of the page also "
        "has enough lines for the gutter-detection code to find here too."
    )
    _para(page, fitz.Rect(50, 90, 290, 440),
          f'<p style="font-size:10pt; text-align:justify;">{left_text}</p>',
          css, archive)
    _para(page, fitz.Rect(320, 90, 560, 440),
          f'<p style="font-size:10pt; text-align:justify;">{right_text}</p>',
          css, archive)
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def build_figure_inline(path):
    """A raster image mid-column with a caption under it."""
    archive, css = font_setup()
    doc = fitz.open()
    page = doc.new_page(width=450, height=650)
    _para(page, fitz.Rect(72, 72, 378, 160),
          '<p style="font-size:11pt; text-indent:18pt; text-align:justify;">'
          "Text before the figure introduces what is about to be shown "
          "and continues for a couple of lines so there is real body "
          "prose above the image in this column.</p>", css, archive)
    # A small synthetic "image": render a simple pixmap and insert it.
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 60))
    pix.set_rect(pix.irect, (200, 200, 220))
    img_rect = fitz.Rect(140, 180, 310, 300)
    page.insert_image(img_rect, pixmap=pix)
    _para(page, fitz.Rect(72, 310, 378, 335),
          '<p style="font-size:9pt; text-align:center;">Figure 1: a caption under the image.</p>',
          css, archive)
    _para(page, fitz.Rect(72, 345, 378, 600),
          '<p style="font-size:11pt; text-indent:18pt; text-align:justify;">'
          "Text after the figure resumes the discussion and continues for "
          "several more lines to give the reflow logic real content below "
          "the obstacle to route around if it ever needs to grow.</p>",
          css, archive)
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def build_figure_vector(path):
    """A chart drawn as many small vector paths (bars of a bar chart)."""
    archive, css = font_setup()
    doc = fitz.open()
    page = doc.new_page(width=450, height=650)
    _para(page, fitz.Rect(72, 72, 378, 110),
          '<p style="font-size:11pt;">A vector chart follows below.</p>', css, archive)
    shape = page.new_shape()
    x = 100
    for h in (40, 70, 55, 90, 30, 60, 80):
        shape.draw_rect(fitz.Rect(x, 400 - h, x + 20, 400))
        x += 30
    shape.finish(color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
    shape.commit()
    _para(page, fitz.Rect(72, 420, 378, 600),
          '<p style="font-size:11pt; text-indent:18pt; text-align:justify;">'
          "Text after the chart resumes the discussion for several lines "
          "so there is real content below this vector obstacle too.</p>",
          css, archive)
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def build_table_ruled(path):
    """A table with drawn borders (rules)."""
    archive, css = font_setup()
    doc = fitz.open()
    page = doc.new_page(width=450, height=400)
    _para(page, fitz.Rect(72, 40, 378, 65),
          '<p style="font-size:11pt;">A ruled table follows.</p>', css, archive)

    rows, cols = 4, 3
    x0, y0, cw, rh = 72, 90, 90, 30
    shape = page.new_shape()
    for r in range(rows + 1):
        shape.draw_line(fitz.Point(x0, y0 + r * rh), fitz.Point(x0 + cols * cw, y0 + r * rh))
    for c in range(cols + 1):
        shape.draw_line(fitz.Point(x0 + c * cw, y0), fitz.Point(x0 + c * cw, y0 + rows * rh))
    shape.finish(color=(0, 0, 0), width=1.0)
    shape.commit()

    headers = ["Year", "Edition", "Price"]
    data = [["1938", "First", "12 marks"], ["1951", "Second", "18 marks"],
            ["1969", "Third", "25 marks"]]
    for c, h in enumerate(headers):
        page.insert_text(fitz.Point(x0 + c * cw + 5, y0 + 20), h, fontsize=10)
    for r, row in enumerate(data, start=1):
        for c, val in enumerate(row):
            page.insert_text(fitz.Point(x0 + c * cw + 5, y0 + r * rh + 20), val, fontsize=10)
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def build_table_borderless(path):
    """A table laid out with whitespace only, no drawn rules."""
    doc = fitz.open()
    page = doc.new_page(width=450, height=400)
    page.insert_text(fitz.Point(72, 60), "A borderless table follows.", fontsize=11)
    headers = ["Year", "Edition", "Price"]
    data = [["1938", "First", "12 marks"], ["1951", "Second", "18 marks"],
            ["1969", "Third", "25 marks"]]
    x0, y0, cw, rh = 72, 100, 90, 24
    for c, h in enumerate(headers):
        page.insert_text(fitz.Point(x0 + c * cw, y0), h, fontsize=10)
    for r, row in enumerate(data, start=1):
        for c, val in enumerate(row):
            page.insert_text(fitz.Point(x0 + c * cw, y0 + r * rh), val, fontsize=10)
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def build_footnotes(path):
    """The existing footnote/reference-list case -- must keep reading as
    single-column, not get misdetected as a table or multi-column page."""
    archive, css = font_setup()
    doc = fitz.open()
    page = doc.new_page(width=450, height=650)
    _para(page, fitz.Rect(72, 72, 378, 200),
          '<p style="font-size:11pt; text-indent:18pt; text-align:justify;">'
          "Body text with a reference to earlier scholarship continues for "
          "a couple of lines before the footnote block begins below on "
          "this single-column academic page.</p>", css, archive)
    baseline = 260.0
    entries = [
        ("1", "Vgl. Horkheimer, Schriften, S. 105-109."),
        ("2", "Adorno, Minima Moralia, S. 12."),
        ("3", "Marcuse, Der eindimensionale Mensch, S. 45."),
    ]
    for marker, entry in entries:
        page.insert_text((72, baseline), marker, fontsize=6.5)
        page.insert_text((79, baseline), f" {entry}", fontsize=9)
        baseline += 11.0
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def build_all():
    CORPUS.mkdir(parents=True, exist_ok=True)
    build_single_column(CORPUS / "single_column.pdf")
    build_two_column(CORPUS / "two_column.pdf")
    build_two_column_heading(CORPUS / "two_column_heading.pdf")
    build_figure_inline(CORPUS / "figure_inline.pdf")
    build_figure_vector(CORPUS / "figure_vector.pdf")
    build_table_ruled(CORPUS / "table_ruled.pdf")
    build_table_borderless(CORPUS / "table_borderless.pdf")
    build_footnotes(CORPUS / "footnotes.pdf")
    print(f"Built corpus in {CORPUS}")


if __name__ == "__main__":
    build_all()
