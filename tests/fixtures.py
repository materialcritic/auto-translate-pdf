"""Builds a synthetic German-academic-style PDF used by test_golden.py.

Deliberately built with raw HTML/CSS through the same insert_htmlbox() +
font_setup() machinery translate_pdf.py itself uses, rather than hand-crafted
span dicts -- that way the fixture's line/span geometry (indentation,
per-span font sizes, italics, line breaks) comes from genuine PyMuPDF HTML
rendering, matching what a real extracted PDF looks like, instead of
something that merely *resembles* one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf as fitz  # noqa: E402

from translate_pdf import font_setup  # noqa: E402

PAGE_W, PAGE_H = 612.0, 792.0
MARGIN_L, MARGIN_R = 72.0, 540.0


def _measure(archive, css, width, html):
    scratch = fitz.open()
    spage = scratch.new_page(width=width + 1, height=3000)
    rect = fitz.Rect(0, 0, width, 3000)
    spare, _ = spage.insert_htmlbox(rect, html, css=css, archive=archive)
    used = 3000 if spare < 0 else 3000 - spare
    scratch.close()
    return used


def build_fixture(path):
    """A one-page synthetic German source PDF exercising, in one page:

    - a heading and two body paragraphs (indentation-based paragraph
      detection, a real italic run, a literal '*' used for a birth date
      right next to that italic run),
    - two back-to-back footnote/reference entries at the body margin, same
      (small) size, separated only by a couple points of gap -- nothing
      but each entry's leading marker span tells them apart -- with the
      first entry's own text hyphenated across a line break,
    - a footer folio deep in the page's bottom margin band.

    Returns the dict of expected/authored y-positions used by the test to
    check the folio wasn't dragged off its page-anchored position.
    """
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    archive, css = font_setup()
    width = MARGIN_R - MARGIN_L
    y = 72.0

    def add(html, gap_after):
        nonlocal y
        h = _measure(archive, css, width, html) + 2
        rect = fitz.Rect(MARGIN_L, y, MARGIN_R, y + h)
        page.insert_htmlbox(rect, html, css=css, archive=archive, scale_low=0)
        y = rect.y1 + gap_after
        return rect

    add('<p style="font-size:14pt;">Einleitung</p>', 20.0)

    add(
        '<p style="font-size:11pt; text-indent:18pt; text-align:justify;">'
        "Der Autor beschreibt die Situation ausfuehrlich im Detail und "
        "erlaeutert zahlreiche Beispiele, wobei die Bedeutung des Werkes "
        "betont wird und viele Zusammenhaenge verdeutlicht werden.</p>",
        14.0,
    )

    add(
        '<p style="font-size:11pt; text-indent:18pt; text-align:justify;">'
        "Vgl. <i>Minima Moralia</i> von Adorno (* 1903) und weitere "
        "Schriften zur kritischen Theorie.</p>",
        30.0,
    )

    # Footnote entries 1 and 2, hand-placed with insert_text() rather than
    # insert_htmlbox(): MuPDF's html engine silently rewrites a literal "-"
    # at a line break into an invisible soft hyphen (U+00AD) even with
    # hyphens:none set (see the "\xad" normalization note in
    # split_page_into_paragraphs) -- fine to correct for when *re*-
    # extracting this pipeline's own output, but it would corrupt the very
    # thing this fixture needs to hand a genuine ASCII "-" to
    # join_paragraph_lines() as if from a real (non-insert_htmlbox-authored)
    # source PDF. insert_text() places literal glyphs with no such rewrite.
    #
    # Same (small) size, same left margin, and a minimal gap between the
    # two entries -- nothing but each entry's leading marker span tells
    # split_page_into_paragraphs() they're separate (Finding 3).
    fn_top = y
    baseline1 = fn_top + 7.0
    page.insert_text((MARGIN_L, baseline1), "1", fontsize=6.5, fontname="Times-Roman")
    page.insert_text((MARGIN_L + 7.0, baseline1), " Vgl. Schulte-",
                      fontsize=9.0, fontname="Times-Roman")
    baseline2 = baseline1 + 11.0
    page.insert_text((MARGIN_L, baseline2), "Sasse, Literarische Theorie, S. 105-109.",
                      fontsize=9.0, fontname="Times-Roman")
    baseline3 = baseline2 + 11.0  # deliberately tiny -- see module docstring
    page.insert_text((MARGIN_L, baseline3), "2", fontsize=6.5, fontname="Times-Roman")
    page.insert_text((MARGIN_L + 7.0, baseline3), " Adorno, Minima Moralia, S. 12.",
                      fontsize=9.0, fontname="Times-Roman")
    y = baseline3 + 20.0

    # Footer folio, deep in the bottom 12% margin band (page_h * 0.88 ~= 697).
    folio_y = 760.0
    folio_rect = fitz.Rect(MARGIN_L, folio_y, MARGIN_R, folio_y + 14.0)
    page.insert_htmlbox(
        folio_rect, '<p style="font-size:9pt; text-align:center;">5</p>',
        css=css, archive=archive, scale_low=0,
    )

    doc.save(str(path))
    doc.close()
    return {"folio_y": folio_y}
