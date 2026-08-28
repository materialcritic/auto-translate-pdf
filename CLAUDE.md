# auto-translate-pdf — working notes

## Ground rules

- The pipeline's existing behaviour on **single-column academic prose is the
  regression baseline**. Any change that alters `--check` output for
  `tests/corpus/single_column.pdf` or `tests/corpus/footnotes.pdf` is a bug
  until proven otherwise.
- Every non-obvious decision in this codebase carries a comment explaining
  the failure it prevents. Match that. A comment saying *what* the code does
  is noise; a comment saying *which bug comes back if you remove this* is the
  house style.
- Never run the model to test a layout change. Use `--check`,
  `--reformat-only`, and `debug_layout.py`. A full translation run is ~7
  minutes on an M1 Air and tells you nothing about geometry.
- PyMuPDF is imported as `import pymupdf as fitz`. Do not "fix" this.
- No new dependencies. PyMuPDF already has image, drawing, and table
  extraction.
- `tests/corpus/*.pdf` are generated, not committed (`*.pdf` is
  gitignored — font substitution differs by machine, which can shift
  bboxes slightly). Run `tests/build_corpus.py` to (re)generate them
  before running `tests/test_regions.py` or `debug_layout.py` against the
  corpus.

## Commands

    ./venv/bin/python tests/build_corpus.py
    ./venv/bin/python translate_pdf.py IN.pdf --check
    ./venv/bin/python debug_layout.py IN.pdf /tmp/overlay.pdf
    ./venv/bin/python tests/test_units.py
    ./venv/bin/python tests/test_golden.py
    ./venv/bin/python tests/test_layout.py
    ./venv/bin/python tests/test_regions.py
    ./venv/bin/python translate_pdf.py IN_en.pdf OUT_en.pdf --reformat-only

## Layout architecture

    page → layout.analyze_page() → {regions, obstacles, tables}
      region   = a flow area. Reflow is scoped to it. One per column/band.
      obstacle = image/figure rect. Text routes around it, redaction skips it.
      table    = translated cell by cell, never reflowed.

Reflow chain state (`prev_orig_y1`, `prev_new_y1`, `prev_was_small_text`,
`prev_was_short`) lives inside `build_region_placements()` and resets at
every call -- i.e. at every region boundary. Carrying it across a column
boundary places column 2's first paragraph off the bottom of the page
(this was a real bug hit and fixed during the layout-support work).

A text region's `rect` from `layout.analyze_page()` is a *tight* bounding
box around whatever content is already there, not the space available for
it to grow into -- `process_pdf` derives its own `fit_rect`/`growth_ceiling`
from it (extending to the next region's top, or the page's bottom margin)
rather than passing the tight rect straight through. Do not "simplify" that
back to just `region["rect"]"`; it silently reintroduces spurious rescaling
and reformat-only idempotency drift.

`find_obstacles()` explicitly excludes solid-white, borderless drawings --
that's what `page.apply_redactions(fill=(1,1,1))` itself bakes into a page
as real vector content once applied, and without the exclusion a second
`--reformat-only` pass (or the watcher re-triggering) detects the previous
pass's own white-out box as a "figure" and skips redacting under it,
leaving old text un-erased underneath new text drawn on top. Do not narrow
or remove that exclusion without re-running the reformat-only idempotency
tests in `tests/test_layout.py` over several passes (not just one -- this
class of bug is invisible after a single pass).

## Things not to do

- Do not replace the paragraph-splitting heuristics with a "cleaner"
  approach. Every guard in `split_lines_into_paragraphs` (`same_row`,
  `starts_with_marker`, `body_size_of_line`) exists because of a specific
  observed failure.
- Do not add a generic recursive XY-cut to `layout.py`. Region boundaries
  are reflow boundaries, and XY-cut produces spurious ones on ordinary
  prose (see `layout.py`'s own module docstring).
- Do not switch table detection to `strategy="text"` by default.
- Do not "simplify" `apply_text_redactions` back to a single union rect.
- Do not touch the `garbage=4, deflate=True` save, the `<end_of_turn>` EOS
  handling, or the asterisk sentinel machinery. None of it is related to
  layout and all of it is load-bearing.
