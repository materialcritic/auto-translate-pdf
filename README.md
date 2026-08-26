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

## Setup

```bash
git clone https://github.com/materialcritic/auto-translate-pdf.git
cd auto-translate-pdf
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

The model (`mlx-community/translategemma-4b-it-4bit`, ~2.1 GB) downloads once
on first run and is cached under `~/.cache/huggingface/hub/` — no re-download
on subsequent runs.

### Standalone usage

```bash
./venv/bin/python translate_pdf.py input.pdf output.pdf [--pages 1-5]
```

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

1. Extract text blocks per page with PyMuPDF, keeping bbox + font info.
2. Split the page's lines into paragraphs using first-line indentation as
   the signal (how justified academic body text is laid out — no blank line
   between paragraphs, just an indent). This runs across *all* of a page's
   lines flattened together (`split_page_into_paragraphs`), not block by
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
non-determinism into text that was already correct). There's no CLI flag for
it yet; call `process_pdf` directly, e.g.:

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

## Known limitations

- **Very short italic phrases** (2-3 words) occasionally lose the `*...*`
  marker during translation and render as plain text — content is never
  lost, just the emphasis styling on that one phrase.
- **Reformat-only mode's tight-gap fix doesn't reach every case.** A title
  page's byline/subtitle stack can still show large gaps even though the
  "short paragraph" tight-gap logic correctly fires for it — the actual
  cause there is `measure_height()` under-computing width for a very narrow
  single-line heading (a short byline like "Peter Stein" can wrap to 2-3
  lines internally at the width it's given, and that wasted internal height
  is what reads as a gap, not the gap logic itself). Not yet fixed —
  isolated to title-page heading stacks, doesn't affect body prose or
  footnote/reference lists.
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
- **Only handles single-column, prose-heavy layouts well.** No table
  support, no multi-column layout support, no image/figure handling.
- Assumes German → English. There's no language auto-detection; dropping a
  non-German PDF will still run it through the DE→EN model.
- Each run loads a ~2 GB model into memory — a full 14-page document took
  about 7 minutes end-to-end on a base M1 Air. Dropping several PDFs in
  quick succession queues them (via a lock file) rather than running
  translations in parallel, so as not to fight over memory/model reloads.

## License

MIT for the code in this repo. The model it downloads and runs
(`mlx-community/translategemma-4b-it-4bit`, derived from Google's Gemma 3) is
distributed separately under its own [Gemma license terms](https://ai.google.dev/gemma/terms).
