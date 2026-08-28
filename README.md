# auto-translate-pdf

Watches a folder and automatically translates any PDF dropped there from
German to English, producing `<name>_en.pdf` alongside the original (which is
left untouched). Translation is layout-preserving: paragraph structure,
headers/footers, footnote reference numbers, and italics (book titles,
emphasis) are all carried over into the English version, not just a plain
text dump.

Everything runs **fully locally and offline** — no API keys, no cloud calls.
Translation is done by [TranslateGemma 4B](https://huggingface.co/google/translategemma-4b-it)
(Google's Gemma 3 fine-tuned for translation), running via
[`mlx-lm`](https://github.com/ml-explore/mlx-lm) on Apple Silicon.

No Apple Silicon handy? [**Open `colab_translate.ipynb` in Colab**](https://colab.research.google.com/github/materialcritic/auto-translate-pdf/blob/main/colab_translate.ipynb)
runs the same pipeline on a free Colab GPU via `transformers` + 4-bit
`bitsandbytes` instead of `mlx-lm` — upload a PDF, run it, download the
result. No Folder Action there (that part's macOS-only); it's a one-document-
at-a-time notebook.

## Setup

```bash
git clone https://github.com/materialcritic/auto-translate-pdf.git
cd auto-translate-pdf
python3 -m venv venv
./venv/bin/pip install -r requirements-mlx.txt
```

`requirements.txt` (just `pymupdf`) is enough on its own for `--check`,
`--reformat-only`, and `tests/test_golden.py` — none of those load a model,
so `mlx-lm` (Apple-Silicon-only) isn't needed for them. Anyone on Linux or
Intel Mac, or a CI runner, can install just `requirements.txt` to run the
tests. `requirements-mlx.txt` pulls in `requirements.txt` plus `mlx-lm` and
`huggingface_hub` for actual translation.

The model (`mlx-community/translategemma-4b-it-4bit`, ~2.1 GB) downloads once
on first run and is cached under `~/.cache/huggingface/hub/` — no re-download
on subsequent runs.

### Standalone usage

```bash
./venv/bin/python translate_pdf.py input.pdf output.pdf [--pages 1-5]
```

Deterministic by default (`--temp 0.0`, seeded RNG) — translation is a
one-right-answer task, not a creative one, so there's usually no reason to
raise the temperature; doing so would also cost the reproducibility that
`--reformat-only` (below) relies on for its own "re-translating would
introduce fresh non-determinism" rationale. `--temp 0.3` (or any non-zero
value) opts into sampling; `--seed N` (default `0`) keeps even a non-zero
`--temp` repeatable run to run.

### Folder-watch usage (macOS Folder Action)

`scripts/auto_translate_pdf.py` watches a folder (`~/Translate` by default —
edit `FOLDER` at the top of the script to change it) and translates any PDF
dropped there. It's designed to be triggered by a macOS Folder Action:

1. In Automator, create a new "Folder Action" workflow attached to your
   watched folder, with a single "Run Shell Script" (`/bin/zsh`) action
   running:
   ```
   /path/to/auto-translate-pdf/venv/bin/python3 /path/to/auto-translate-pdf/scripts/auto_translate_pdf.py
   ```
2. Save the workflow, then attach it to the folder (Folder Actions run when
   Finder detects a new item added to the attached folder — this also fires
   for files added via a script or `cp`, not just drag-and-drop).

It's also safe to just run the script directly/on a cron — it only touches
files it hasn't successfully translated before (tracked in a `.auto_translate_state.json`
file inside the watched folder), and a lock file keeps concurrent triggers
from racing each other or double-loading the model.

## How the translation pipeline works (`translate_pdf.py`)

Each page is first split by `layout.analyze_page()` (see "Layout support"
above) into regions (flow areas — a column, a full-width heading band),
obstacles (images/figures text must route around), and tables. The steps
below happen *within* each text region — a single-column page with no
figures or tables produces exactly one region, so this reads the same as
before that layer existed for that (still the common) case.

1. Extract text blocks per page with PyMuPDF, keeping bbox + font info.
2. Split each region's lines into paragraphs using first-line indentation as
   the signal (how justified academic body text is laid out — no blank line
   between paragraphs, just an indent). This runs across *all* of a region's
   lines flattened together (`split_lines_into_paragraphs`), not block by
   block — some PDF producers group a whole paragraph into one PDF "block"
   (block-level detection would suffice there), but others emit one block
   per line, in which case per-block detection can never see a previous
   line to compare indentation against, and every line gets mistaken for
   its own paragraph. Flattening first makes the two cases indistinguishable.
   Two more lines-that-look-like-paragraph-starts get guarded against:
   - A large font-size jump forces a break too (so a heading dropped into
     body text doesn't get swallowed into the surrounding paragraph) —
     except this is computed ignoring footnote-marker spans, since a line
     consisting only of a trailing footnote marker (a lone superscript "1"
     that landed on its own line at a paragraph's tail) would otherwise
     report that marker's tiny font size, look like a spurious size jump,
     and get sliced into its own degenerate one-token "paragraph".
   - Lines that land at (almost) the *same* y-coordinate as the previous
     line are always treated as the same physical line continuing, no
     matter what their x0/size look like. Some PDFs (justified text with
     unusually wide word-cluster gaps) get split by PyMuPDF into several
     "line" dict entries that are all really one visual line — without this
     guard, each word cluster's x0 looks like an indent and the whole line
     gets shattered into one-word "paragraphs" translated in isolation.
3. Two kinds of inline markup get rewritten as plain-text markers before
   translation, since a translation model only ever sees a flat string and
   any real character-level formatting would otherwise be silently lost:
   - A footnote reference number (a separate, smaller-font span glued
     directly onto the preceding word, e.g. "1938" + superscript "1") is
     rewritten as an explicit `[1]` token — otherwise it gets absorbed into
     the adjacent number (`"19381"`) and becomes unrecoverable.
   - An italic span is wrapped in `*asterisks*`. TranslateGemma reliably
     passes this markdown-style emphasis through translation intact
     (confirmed empirically — it already produces this style on its own for
     things like book titles).
4. Translate each paragraph with TranslateGemma via `mlx_lm.generate`. Note:
   TranslateGemma's chat template requires a specific structured message
   format (`{"type": "text", "source_lang_code": "de", "target_lang_code":
   "en", "text": ...}`) — it does not accept a system prompt or free-form
   instructions, unlike a general chat model.
5. If a `[N]` footnote marker got dropped by translation, it's appended at
   the end of the paragraph rather than silently lost (imperfect placement,
   but nothing disappears). Separately, a paragraph with almost no real text
   after stripping markers (fewer than 4 characters — e.g. a stray dash, or
   a footnote marker that still ended up alone despite the guards above) is
   never sent to the model at all and is kept as-is: a translation request
   with nothing real to translate risks getting back a confused
   conversational reply ("please provide the German text...") instead of an
   actual translation, which would otherwise get inserted into the PDF as
   if it were real content.
6. Reflow: paragraphs are reassembled page-wide (not block-by-block). Each
   paragraph's gap to the *next* one is computed from the original document
   (`next.y0 - this.y1`) and applied unchanged after the actual rendered
   bottom of this paragraph (`new_y0 = prev_new_y1 + original_gap`) — so a
   paragraph that translates shorter or longer pushes everything below it up
   or down, but the *spacing* it leaves behind matches the original layout's
   intent exactly. An earlier version instead accumulated every paragraph's
   own height delta into one running offset applied to each paragraph's
   original y0; that let a paragraph's shrinkage inflate the gap *after* it,
   which is invisible for one paragraph but compounds down a references list
   where every entry translates shorter than the German, producing
   obviously-oversized gaps between every single footnote.
7. Redact the original page's entire text footprint in one shot, then
   re-insert the English text via `page.insert_htmlbox()` (not the simpler
   `insert_textbox`) so that `*italic*` markers can become real `<i>` runs,
   using an embedded Times New Roman + Times New Roman Italic (via
   `fitz.Archive`) — PyMuPDF's built-in base-14 fonts only cover a narrow
   glyph set and silently render smart quotes/em dashes as `?`.
8. A paragraph that was a single line in the source (headings, bylines) is
   left-aligned instead of justified, and given a small width buffer — the
   HTML box model's font metrics differ slightly from `insert_textbox`, and
   justifying a short heading that wraps produces obviously-stretched
   letter-spacing.
9. The document is saved with `garbage=4, deflate=True`. Without this, each
   `insert_htmlbox` call embeds its own font subset rather than reusing one
   already embedded on the page — across a 41-page document that was 1,143
   duplicate embedded font objects, ballooning a file that should be ~1MB
   into ~950MB. `garbage=4` finds and merges/drops the duplicates on save.

## Reformat-only mode (fix layout without re-translating)

`process_pdf(in_path, out_path, model_name, skip_translation=True)` re-reads
an *already-English* PDF this script produced and re-runs only the
extraction/reflow/rendering steps — no model load, no translation calls. Use
this for a pure formatting fix on an existing output (e.g. after a gap-math
or rendering bug fix lands, to fix a file translated before the fix without
paying for a fresh translation, which would also introduce fresh
non-determinism into text that was already correct):

```bash
./venv/bin/python translate_pdf.py in_en.pdf out_en.pdf --reformat-only
```

or, calling `process_pdf` directly:

```python
import sys; sys.path.insert(0, ".")
from translate_pdf import process_pdf
process_pdf("in_en.pdf", "out_en.pdf", None, skip_translation=True)
```

Two things only matter in this mode:
- `dehyphenate` is forced off. `insert_htmlbox` never hyphenates at a real
  line break the way German typesetting does, so a "-" in re-extracted text
  is always a real character (a compound word, a name, a page range) — the
  normal de-hyphenation rule would wrongly delete it ("Schulte-Sasse" ->
  "SchulteSasse").
- Consecutive paragraphs identified as "tight" (either both smaller than
  the page's largest font size, i.e. a footnote/reference-list run, or both
  short — under 120 characters, catching title/subtitle fragments and
  citation-metadata lines) get a fixed 3pt gap instead of the gap preserved
  from the input file, since in this mode "the original gap" just means
  whatever's already baked into the file being reformatted — for these runs
  that's the exact oversized/compounding gap being fixed, not a genuine
  layout intent to preserve. `page_body_size` for the small-text check is
  the *largest* font size on the page, not the most common one — a page
  with more (short) footnote paragraphs than (long) body paragraphs would
  otherwise make the footnote size look like "the body size" by paragraph
  count alone.

## Sanity-checking a document (`--check`)

```bash
./venv/bin/python translate_pdf.py input.pdf --check
```

Reports, per page, the paragraph count, the detected body font size, the
modal left margin, the near-empty-paragraph count, and (see "Layout
support" below) the region/column/obstacle/table counts — the same signals
`split_lines_into_paragraphs`/`layout.analyze_page`/`process_pdf` use
internally — without loading the model, translating anything, or writing
any output. Useful for sanity-checking how those heuristics are reading an
unfamiliar document before committing to a full, slow, model-backed run: a
paragraph count wildly out of proportion to the page's visible content, a
near-empty count that's suspiciously high, a modal margin that doesn't
match the visible body text, or a column/table count that doesn't match
what the page actually looks like all point at the same handful of
extraction assumptions not fitting this particular PDF's layout. `output`
isn't required in this mode.

## Layout support (columns, images, tables)

The pipeline is layout-*safe* rather than layout-*aware* in the sense a
commercial tool is: it correctly handles the large majority of real
academic-document layouts and, where it doesn't understand something,
leaves that part alone instead of producing confident, fluent, wrong
output.

`layout.py` splits each page into `regions` (ordered flow areas — one per
column, or a full-width heading band spanning several), `obstacles`
(images and clustered vector figures that text must never be drawn over or
redacted through), and `tables` (translated cell by cell, never reflowed).
A single-column page with no figures or tables — most of the documents
this pipeline was originally built for — still produces exactly one
region, so nothing changes for that case.

- **Columns**: detected via real vertical gutters (not a generic
  whitespace-valley cut, which would fire on every wide paragraph gap in
  ordinary prose), with a full-width heading correctly read as its own
  region spanning the columns below it. Reflow is scoped to each column
  independently, so a paragraph that grows pushes down only its own
  column. Handles the large majority of two/three-column academic layouts;
  it will fail on layouts with unequal column counts per band that aren't
  separated by a spanning heading, and on newspaper-style mixed-width
  columns. `--check` reports the detected column count; there's no
  `--force` needed anymore (multi-column pages are translated properly).
- **Images/figures**: raster images and vector charts (bar charts, plots —
  clustered from potentially hundreds of individual drawn paths into one
  obstacle) are detected and text is reflowed around them; redaction is
  now per-paragraph rather than one union rect for the page, so a figure
  sitting inside the text column's bounding box is never covered by a
  white redaction rectangle. Caption anchoring is the fiddly residual — a
  caption is a short paragraph that happens to sit under a figure, and
  nothing in the PDF itself says so.
- **Tables**: `--table-strategy lines` (the default) needs drawn borders;
  `--table-strategy text` also catches borderless tables but false-
  positives on reference lists and other short-paragraph runs, so it's an
  explicit opt-in. `--no-tables` disables table detection entirely (table
  text then flows as ordinary prose, which will scramble it — only useful
  as a fallback if detection is misfiring on a specific document). Quality
  is capped by PyMuPDF's own `find_tables()`, not by this pipeline; ruled
  tables work well.
- **Debugging a layout issue**: `debug_layout.py IN.pdf overlay.pdf` draws
  the detected regions (blue), tables (green), obstacles (red), and column
  gutters (orange) onto a copy of the PDF — tuning gutter/figure-clustering
  behavior by reading paragraph counts on stderr doesn't work; look at the
  boxes.

## Testing

`tests/test_golden.py` is a golden-file regression test: it builds a
synthetic fixture PDF (see `tests/fixtures.py`) exercising the trickier
corners of the pipeline — footnote-marker splitting, hyphenation across a
line break, a literal asterisk next to a real italic run, and a
page-anchored folio — and runs it through `process_pdf` with `load_model`/
`translate` stubbed out (no model download, no GPU/Apple Silicon needed, no
non-determinism from an actual LLM). Run it directly:

```bash
./venv/bin/python tests/test_golden.py
```

No test framework dependency; it prints PASS/FAIL per check and exits
non-zero if anything fails. `tests/test_units.py` and `tests/test_layout.py`
follow the same style, covering the pure functions and layout-correctness
regressions (overflow/pinning, reformat-only idempotency, multi-column
translation, table cell translation, figure/obstacle routing) respectively.

`tests/test_regions.py` tests `layout.py` directly against a small
generated corpus. Build the corpus first (not committed — `*.pdf` is
gitignored, since it's generated via `font_setup()`'s system font
resolution, which differs by machine):

```bash
./venv/bin/python tests/build_corpus.py
./venv/bin/python tests/test_regions.py
```

## Known limitations

- **Very short italic phrases** (2-3 words) occasionally lose the `*...*`
  marker during translation and render as plain text — content is never
  lost, just the emphasis styling on that one phrase.
- **Bold is not carried over — only italics.** A bold heading or a bold
  emphasis run renders as plain regular text; if TranslateGemma itself
  emits markdown `**bold**` in its output, it renders as *italic* instead
  (the pipeline only distinguishes "emphasized" from "not," via the same
  `*...*` marker italics already use — a real bold/italic distinction would
  need its own marker, its own font-face pair, and its own HTML tag, which
  hasn't been built). Content is never lost, only that one styling
  distinction.
- **Every paragraph renders in one serif typeface** (`font_setup()`'s
  resolved roman+italic pair — Times New Roman on macOS, Liberation Serif
  elsewhere), regardless of what font(s) the source document actually used.
  A sans-serif heading or a source set in a different serif family both
  come out in the same face as the body text. Defensible for this
  project's target document class (academic prose is reliably one serif
  family throughout a given document) but worth knowing before pointing
  this at something typographically mixed.
- **A hyphenated word split across a line break can leave a residual
  fragment if the split happens right at a paragraph-merge boundary edge
  case not yet covered** (the common case — including hyphenation across an
  italic-run boundary — is handled, but this is heuristic-based extraction
  from arbitrary PDF layouts, not a guarantee against every possible
  producer quirk). Spot-check unfamiliar documents' output, especially
  around footnotes and section breaks.
- **Translation quality** depends entirely on TranslateGemma 4B. It reads as
  fluent, idiomatic English on academic German prose, but isn't a substitute
  for professional translation of anything high-stakes.
- **Column, image, and table support is layout-safe, not layout-aware**
  (see "Layout support" above) — it handles the large majority of academic
  layouts correctly, but a layout it doesn't understand is left alone
  rather than translated confidently wrong. Figures aren't captioned. A
  paragraph that starts at the bottom of one column and continues at the
  top of the next is currently translated as two disconnected fragments,
  each without the other's context (splitting the translated English back
  across the column break to preserve that continuity is the one part of
  this design not built).
- **Link and form-widget annotations aren't updated after reflow.** A link
  or widget's rect still points at wherever the *original* German text sat;
  once a paragraph grows, shrinks, or shifts, that rect no longer lines up
  with the translated text drawn in its place. The document's `/Title`
  metadata is translated (nothing else — outline/bookmarks and other
  metadata fields stay German).
- Assumes German → English. There's no language auto-detection; dropping a
  non-German PDF will still run it through the DE→EN model.
- A source PDF genuinely named to end in `_en` (e.g. `Anhang_en.pdf`, not a
  prior translation output) is treated as already-translated and skipped by
  the watcher with no message — `is_translated_output`'s suffix check has
  no way to tell the two cases apart.
- Each run loads a ~2 GB model into memory — a full 14-page document took
  about 7 minutes end-to-end on a base M1 Air. Dropping several PDFs in
  quick succession queues them (via a lock file) rather than running
  translations in parallel, so as not to fight over memory/model reloads.

## License

MIT for the code in this repo. The model it downloads and runs
(`mlx-community/translategemma-4b-it-4bit`, derived from Google's Gemma 3) is
distributed separately under its own [Gemma license terms](https://ai.google.dev/gemma/terms).

## Change history (post-publish fixes)

An external audit found several real bugs after this was first published,
most consequentially:

- **A phantom `2em` vertical margin on every paragraph.** `BODY_CSS` reset
  margins with a universal selector (`* { margin: 0 }`), but that has CSS
  specificity 0 and loses to MuPDF's user-agent `p { margin: 1em 0 }` --
  every paragraph silently carried an extra 2× its own font size in blank
  space. This was the true root cause of the "oversized gap between every
  footnote" bug fixed earlier, and of what this file previously documented
  as a separate, unfixed "title-page heading stack" spacing bug — that
  diagnosis (a width-measurement issue) was wrong; it was this margin all
  along. Fixed with an explicit `p, div, body { margin: 0 }` rule.
- **Bare page numbers were being deleted**, not left untranslated as
  intended — their bbox was folded into the page-wide redaction, but the
  `continue` that skipped translating them also skipped placing them back.
- **De-hyphenation ran as a global regex over already-joined paragraph
  text**, matching *any* hyphen followed by whitespace rather than only one
  that ended a source line -- corrupting ordinary German suspended
  compounds ("Sozial- und Wirtschaftsgeschichte" -> "Sozialund
  Wirtschaftsgeschichte") and dash-as-punctuation usage, not just an edge
  case at paragraph-merge boundaries. Rewritten to de-hyphenate at the line
  join, where line boundaries still exist.
- **The page-wide text redaction was also deleting embedded images**
  (PyMuPDF's default redaction blanks image pixels intersecting the
  redaction rect). Now scoped to text only.
- **`fitz.Archive` ran at module import time**, so a missing font directory
  (any non-macOS system, or a machine without these specific fonts) made
  the whole module unimportable with a traceback that a Folder Action would
  swallow silently. Now built lazily on first use, with a fallback font
  list and an actionable error.

Also fixed: same-row line fragments could be joined out of left-to-right
order; the watcher now persists progress after every file (was: only at the
end of a whole run) and doesn't retire a permanently-failing file into the
same "done" bucket as a real success; a file mid-copy is no longer grabbed
and burned as a false failure; several smaller robustness/hygiene issues
(case-sensitive `.pdf` matching, a state file that could `KeyError` on an
older schema, CSV log rows breaking on multi-line error text, the
now-deprecated `import fitz`, and an `hf_transfer` dependency that never
actually activated).

### Follow-up: page-number regression from the first patch

A second audit pass caught a regression in the page-number fix above: once
folios started calling `place()` instead of being silently dropped, they
joined the same reflow chain as body text — a folio is anchored to the
*page*, not to the text flow, so inheriting the body's accumulated shift
could move it hundreds of points, and (worse) a header folio being the
*first* paragraph on the page made it the anchor that pushed all the body
text below it up by tens of points on every page with a running head.
Fixed by pinning any digit-only paragraph inside the top/bottom 12% margin
band to its original position, entirely outside the `prev_orig_y1`/
`prev_new_y1` chain. Verified against both the footer-folio and
header-folio fixtures from the report.
