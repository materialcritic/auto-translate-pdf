# auto-translate-pdf — pipeline overview & audit/fix history

This is the detailed technical record of the whole project: what the
pipeline does and how it works end to end, plus everything three rounds of
external audit found and how each fix was verified. `README.md` carries a
shorter version of the pipeline mechanics for a first-time reader; this file
is the fuller reference.

---

## What this is

A local, offline German → English PDF translator. Drop a PDF in, get back
`<name>_en.pdf` with the same layout — paragraph structure, headers/footers,
footnote reference numbers, and italics (book titles, emphasis) all carried
over, not just a flat text dump.

Everything runs on-device: translation is done by
[TranslateGemma 4B](https://huggingface.co/google/translategemma-4b-it)
(Google's Gemma 3 fine-tuned for translation), running locally via
[`mlx-lm`](https://github.com/ml-explore/mlx-lm) on Apple Silicon. No API
keys, no cloud calls, no per-page cost. `colab_translate.ipynb` in this repo
runs the same pipeline on a free Colab GPU instead, via `transformers` + 4-bit
`bitsandbytes`, for anyone without Apple Silicon handy — same model family,
same chat-template format, just a different `generate()` call and no Folder
Action (that part's macOS-only).

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
dropped there, hands-off. It's designed to be triggered by a macOS Folder
Action:

1. In Automator, create a new "Folder Action" workflow attached to your
   watched folder, with a single "Run Shell Script" (`/bin/zsh`) action
   running:
   ```
   /path/to/auto-translate-pdf/venv/bin/python3 /path/to/auto-translate-pdf/scripts/auto_translate_pdf.py
   ```
2. Save the workflow, then attach it to the folder (Folder Actions run when
   Finder detects a new item added to the attached folder — this also fires
   for files added via a script or `cp`, not just drag-and-drop).

It's also safe to just run the script directly, or on a cron — it only
touches files it hasn't successfully translated before (tracked in a
`.auto_translate_state.json` file inside the watched folder, keyed on each
file's path *and* size/mtime — see Finding 10 below — so replacing a file
under the same name gets picked up rather than skipped forever), and a lock
file keeps concurrent triggers from racing each other or double-loading the
model. `wait_until_stable()` also holds off on a file until its size has
stopped changing for a couple of polls, since a Folder Action fires on "item
added," which for a large or network-copied file can precede the last byte
actually landing.

## How the translation pipeline works (`translate_pdf.py`)

1. **Extract** text blocks per page with PyMuPDF, keeping bbox + font info
   for every span.
2. **Split into paragraphs.** `split_page_into_paragraphs` groups a page's
   lines using indentation as the primary signal — how justified academic
   body text is laid out: no blank line between paragraphs, just an indented
   first line. This runs across *all* of a page's lines flattened together,
   not block by block: some PDF producers group a whole paragraph into one
   PDF "block" (block-level detection would suffice there), but others emit
   one block per line, in which case per-block detection can never see a
   previous line to compare indentation against, and every line would be
   mistaken for its own paragraph — destroying sentence structure at every
   mid-sentence line break. Flattening first makes the two cases
   indistinguishable, which is what's wanted.

   Several more signals guard against merging things that just happen to
   share the body margin, or splitting things that shouldn't be split:
   - A large font-size jump forces a paragraph break (a heading dropped into
     body text shouldn't get swallowed into the surrounding paragraph) —
     computed while ignoring footnote-marker spans specifically, since a
     line consisting only of a trailing footnote marker would otherwise
     report that marker's tiny font size as a spurious size jump and get
     sliced into its own degenerate one-token "paragraph."
   - Lines at (almost) the same y-coordinate as the previous line are always
     treated as the same physical line continuing, regardless of x0/size.
     Some PDFs (justified text with unusually wide word-cluster gaps) get
     split by PyMuPDF into several "line" dict entries that are really one
     visual line — without this guard, each word cluster's x0 would look
     like an indent and shatter the line into one-word "paragraphs"
     translated in isolation, scrambling the sentence.
   - A line that *opens* with a footnote reference marker also forces a new
     paragraph — footnote/reference-list entries sit flush at the body
     margin, same size, one after another with normal line spacing, so none
     of the other signals fire between consecutive entries; without this,
     a whole block of footnotes gets merged and translated as one run-on
     blob.
3. **Mark up inline formatting as plain-text tokens.** A translation model
   only ever sees a flat string, so any real character-level formatting has
   to survive as literal characters or it's lost:
   - A footnote reference number (a separate, smaller-font span glued
     directly onto the preceding word, e.g. "1938" + superscript "1") is
     rewritten as an explicit `[1]` token — otherwise it gets silently
     absorbed into the adjacent number (`"19381"`) and becomes
     unrecoverable. Unicode superscript digits (¹²³...) are normalized to
     ASCII first, since a raw `[¹]` could never be matched by the regex that
     looks for it again later.
   - An italic span is wrapped in `*asterisks*`. TranslateGemma reliably
     passes this markdown-style emphasis through translation intact
     (confirmed empirically — it already produces this style on its own for
     things like book titles). A literal `*` already present in the source
     (German uses `* 1903` for a birth date; plain arithmetic like `2 * 4`
     also occurs) would otherwise be indistinguishable from an italic
     delimiter once spans are flattened to a string, so it's parked on a
     private-use Unicode sentinel first and restored to a literal `*` only
     at render time.
4. **Translate** each paragraph with TranslateGemma via `mlx_lm.generate`.
   TranslateGemma's chat template requires a specific structured message
   format (`{"type": "text", "source_lang_code": "de", "target_lang_code":
   "en", "text": ...}`) — it does not accept a system prompt or free-form
   instructions, unlike a general chat model.
5. **Recover anything the model dropped.** If a `[N]` footnote marker didn't
   survive translation verbatim, it's appended at the end of the paragraph
   rather than silently lost (imperfect placement, but nothing disappears —
   and this check is count-aware, so a paragraph carrying `[1]` twice only
   treats it as complete if *both* survived). Separately, a paragraph with
   almost no real text after stripping markers (fewer than 4 characters —
   e.g. a stray dash, or a footnote marker that still ended up alone despite
   the paragraph-merge guards above) is never sent to the model at all: a
   translation request with nothing real to translate risks getting back a
   confused conversational reply ("please provide the German text...")
   instead of an actual translation, which would otherwise get inserted into
   the PDF as if it were real content.
6. **Reflow page-wide**, not block-by-block. Each paragraph's gap to the
   *next* one is computed from the original document (`next.y0 - this.y1`)
   and applied unchanged after the actual rendered bottom of this paragraph
   (`new_y0 = prev_new_y1 + original_gap`) — so a paragraph that translates
   shorter or longer pushes everything below it up or down, but the spacing
   it leaves behind matches the original layout's intent exactly, rather
   than compounding a paragraph's own shrinkage into the gap after it.
   Page-anchored furniture (folios, running heads — digit-only paragraphs
   sitting in the top/bottom 12% margin band of the page) is *pinned*
   instead: kept at its original position and excluded from this chain
   entirely, since a folio is anchored to the page, not to the text flow,
   and would otherwise inherit the body's accumulated shift (or, worse, if
   it's the first paragraph on the page, *become* the chain's anchor and
   drag the entire body up or down with it).
7. **Clamp to the page.** `fit_placements_to_page` runs after every
   paragraph on a page has been placed, and before redaction: if the
   English runs long enough that the flowing placements would extend past
   the page bottom, it first shrinks the inter-paragraph gaps (never below a
   floor), and if that alone isn't enough, scales every flowing paragraph's
   font down by one uniform factor (so the page stays visually consistent
   rather than one arbitrary paragraph shrinking) and re-measures. Pinned
   placements are left untouched throughout, since they're page-anchored,
   not flow-anchored. If even the minimum scale won't fit, the text is laid
   out clipped at that scale and a warning is reported rather than the
   overflow simply vanishing off-page with no error at all.
8. **Redact and re-insert.** The original page's entire text footprint is
   redacted in one shot (scoped to text only — `PDF_REDACT_IMAGE_NONE` /
   `PDF_REDACT_LINE_ART_NONE`, so embedded images/figures on the page
   survive untouched), then the English text is re-inserted via
   `page.insert_htmlbox()` (not the simpler `insert_textbox`) so that
   `*italic*` markers can become real `<i>` runs, using an embedded Times
   New Roman + Times New Roman Italic (via `fitz.Archive`, built lazily on
   first use with a fallback list of font locations) — PyMuPDF's built-in
   base-14 fonts only cover a narrow glyph set and silently render smart
   quotes/em dashes as `?`.
9. **Save.** `garbage=4, deflate=True` finds and merges duplicate embedded
   font objects — without it, each `insert_htmlbox` call embeds its own font
   subset rather than reusing one already on the page, which across a
   41-page document produced 1,143 duplicate font objects and ballooned a
   file that should be ~1MB into ~950MB. Saved to a `.part` temp name and
   renamed on success, so a mid-run failure never leaves a corrupt or
   partial file where the watcher's `out.exists()` check would mistake it
   for a completed translation.

## Reformat-only mode (fix layout without re-translating)

`process_pdf(in_path, out_path, model_name, skip_translation=True)`
(or `translate_pdf.py ... --reformat-only` from the CLI) re-reads an
*already-English* PDF this script produced and re-runs only the
extraction/reflow/rendering steps — no model load, no translation calls. Use
this for a pure formatting fix on an existing output (e.g. after a
rendering-bug fix lands, to fix a file translated before the fix without
paying for a fresh translation, which would also introduce fresh
non-determinism into text that was already correct):

```python
import sys; sys.path.insert(0, ".")
from translate_pdf import process_pdf
process_pdf("in_en.pdf", "out_en.pdf", None, skip_translation=True)
```

Two things only matter in this mode:
- `dehyphenate` is forced off. `insert_htmlbox` never hyphenates at a real
  line break the way German typesetting does, so a "-" in re-extracted text
  is always a real character (a compound word, a name, a page range) — the
  normal de-hyphenation rule would wrongly delete it. Reformat mode instead
  keeps the hyphen and joins the two halves without inserting a space.
- Consecutive paragraphs identified as "tight" (either both smaller than the
  page's largest font size, i.e. a footnote/reference-list run, or both
  short — under 120 characters, catching title/subtitle fragments and
  citation-metadata lines) get a fixed 3pt gap instead of the gap preserved
  from the input file, since in this mode "the original gap" just means
  whatever's already baked into the file being reformatted — for these runs
  that can be exactly the oversized/compounding gap a fix is meant to
  correct, not a genuine layout intent to preserve.

## Known limitations

- **Very short italic phrases** (2-3 words) occasionally lose the `*...*`
  marker during translation and render as plain text — content is never
  lost, just the emphasis styling on that one phrase.
- **A hyphenated word split across a line break can leave a residual
  fragment** in a paragraph-merge edge case not yet covered (the common
  case, including hyphenation across an italic-run boundary, is handled) —
  this is heuristic-based extraction from arbitrary PDF layouts, not a
  guarantee against every possible producer quirk. Spot-check unfamiliar
  documents' output, especially around footnotes and section breaks.
- **Translation quality** depends entirely on TranslateGemma 4B. It reads as
  fluent, idiomatic English on academic German prose, but isn't a substitute
  for professional translation of anything high-stakes.
- **Only handles single-column, prose-heavy layouts well.** No table
  support, no multi-column layout support. Embedded images/figures are left
  untouched but aren't captioned or otherwise processed.
- Assumes German → English. There's no language auto-detection; dropping a
  non-German PDF will still run it through the DE→EN model.
- Each run loads a ~2 GB model into memory — a full 14-page document took
  about 7 minutes end-to-end on a base M1 Air; a 40-page document took ~49
  minutes on the same machine under memory pressure from other running
  apps. Dropping several PDFs in quick succession queues them (via a lock
  file) rather than running translations in parallel, so as not to fight
  over memory/model reloads.

## License

MIT for the code in this repo. The model it downloads and runs
(`mlx-community/translategemma-4b-it-4bit`, derived from Google's Gemma 3) is
distributed separately under its own
[Gemma license terms](https://ai.google.dev/gemma/terms).

---

# Audit & fix history

Three rounds of external audit have been run against this pipeline since the
initial publish. What follows is the detailed record of what each one found,
what was actually fixed, and how each fix was verified.

---

## Round 1 — initial audit (20 findings)

Commit: `7a91255` "Fix bugs found by external audit" (baseline `586d887`).

Full findings list and fixes are already summarized in `README.md`'s
"Change history" section. Headline items:

1. **Phantom `2em` vertical margin on every paragraph.** `BODY_CSS` reset
   margins with `* { margin: 0 }`, which has CSS specificity 0 and lost to
   MuPDF's user-agent `p { margin: 1em 0 }`. Verified: `measure_height` for
   a single line went from `3.2 × size` to `1.2 × size` after adding an
   explicit `p, div, body { margin: 0 }` rule. This turned out to be the
   *actual* root cause of two things that had separately been diagnosed
   (and one of them shipped as a documented, unfixed "limitation"):
   - the "oversized gap between every footnote" bug fixed earlier in this
     project's own history, and
   - a title-page byline/subtitle spacing issue that had been attributed to
     a `measure_height()` width-measurement problem. That diagnosis was
     wrong; it was this margin all along, and it disappeared on its own
     once the CSS was fixed (confirmed by re-rendering the same title page).
2. **Bare page numbers were being deleted**, not left untranslated as the
   code comment claimed. Their bbox was folded into the page-wide redaction
   rect, but the `continue` that skipped translating them also skipped
   `place()`-ing them back — so they were whited out with nothing drawn in
   their place.
3. **De-hyphenation ran as a global regex over already-joined paragraph
   text** (`re.sub(r"-\s+", "", text)`), matching *any* hyphen followed by
   whitespace — not just one that ended a source line. This corrupted
   ordinary German suspended compounds ("Sozial- und Wirtschaftsgeschichte"
   → "Sozialund Wirtschaftsgeschichte"), page ranges ("105-" + "109" →
   "105109"), and hyphens used as dashes (deleted outright). Rewritten as
   `join_paragraph_lines()`, which de-hyphenates at the line join, where
   line boundaries still exist, with a `_SUSPENSION_RE` guard so "Sozial-"
   followed by "und"/"oder"/etc. keeps its hyphen and space.
4. **Page-wide text redaction was also deleting embedded images** — PyMuPDF
   defaults to `PDF_REDACT_IMAGE_PIXELS`, which blanks any image
   intersecting the redaction rect, and that rect spans the whole text
   column. Fixed with `images=PDF_REDACT_IMAGE_NONE,
   graphics=PDF_REDACT_LINE_ART_NONE`.
5. **`fitz.Archive` ran at module import time**, so a missing font
   directory (any non-macOS machine, or a Mac without these exact fonts)
   made the whole module unimportable with a traceback a Folder Action
   would silently swallow. Replaced with `font_setup()`, built lazily on
   first use with a fallback list of font locations (macOS, generic Linux
   msttcorefonts, Liberation) and a `RuntimeError` with an actionable
   message if none resolve.

Plus 15 further findings (same-row line-fragment reading order, the EOS
token registration comment being aspirational rather than true at the time,
`prev_context` being a dead parameter, footnote-marker multiplicity in
`preserve_footnote_markers`, CSV log rows breaking on multi-line error text,
the deprecated `import fitz`, an inert `hf_transfer` env var, `parse_page_range`
throwing raw tracebacks instead of a usage error, `process_pdf` never closing
its input document, case-sensitive `.pdf` matching in the watcher, a watcher
state file that could `KeyError` on an older schema, the watcher persisting
state only at the very end of a run instead of after each file, the watcher
grabbing still-copying files and burning real attempts on them, `**bold**`
producing a stray literal asterisk, and no `--reformat-only` CLI flag) — all
addressed in the same commit; see `README.md` for the full prose.

---

## Round 2 — follow-up audit (regression found)

Commit reviewed: `7a91255`. Fix commit: `4090c49`.

**19 of 20 round-1 findings verified clean.** One fix had introduced a
regression worse than the bug it replaced:

### The regression

Finding 2's fix (page numbers must call `place()`, not `continue`) meant a
folio now joined the same reflow chain as body text. But a folio is
anchored to the *page*, not to the text flow:

- A **footer folio** inherited the body's accumulated shift and could land
  hundreds of points away from its footer position (measured: 400pt up the
  page in the reported fixture).
- A **header folio** is the *first* paragraph on the page, so it became the
  chain's *anchor* — every body paragraph below it then inherited an offset
  derived from the folio's own position, shifting the entire body up by
  tens of points on **every page with a running head**, which is most
  academic PDFs. This was a strictly bigger problem than the missing folio
  had been before Finding 2's own fix.

### The fix

Digit-only paragraphs (folios, running heads matched by the page-number
regex) sitting inside the top/bottom 12% margin band of the page are now
**pinned**: they keep their original y-position and are excluded from the
`prev_orig_y1`/`prev_new_y1` reflow chain entirely, so they neither inherit
the body's shift nor pass a gap on to whatever follows them.

**Verified** against both the footer-folio and header-folio fixtures from
the report: with the fix, a folio at source y=30.5 rendered at y=30.6, body
text starting at source y=78.4 rendered at y=78.5 (vs. a 32pt shift before
the fix), and a footer folio at source y=543.4 rendered at y=543.5 (vs.
landing at y=144.0, ~400pt up the page, before the fix).

---

## Round 3 — fresh audit with a synthetic-PDF test harness (10 findings)

Commit reviewed: `main` (i.e. everything through round 2's fix). This audit
built a synthetic German academic PDF (indented body paragraphs, a 14pt
heading, an italic book title, superscript footnote markers, a 9pt footnote
block, a folio in the footer band), stubbed `mlx_lm` to drive `translate()`
through identity/grown/shrunk/marker-dropping outputs, and ran both the
translate path and `--reformat-only` against it.

**Headline finding: on a page where the English overflows the page bottom,
the excess text is silently invisible in the output — no error, no
warning.** In the audit's heaviest test case, 73% of the translated content
on the page was lost this way.

Fixed in dependency order **6 → 4 → 5 → 2 → 3 → 7 → 1** (this order matters:
3 depends on `span_is_footnote_marker` from 6, and 1 depends on the
dict-shaped placements introduced generally), then **8** as a standalone
patch. **9** and **10** below are documented for completeness but were not
both applied here.

### 1. Critical — translated text overflowing the page is silently lost — Fixed + verified

`place()` preserves each paragraph's exact original gap to the next, which
is correct, but nothing in that chain knows where the page ends. When
English runs longer than German, every paragraph below the growth point is
pushed down, and once a rect passes the media box, `insert_htmlbox` still
reports a clean fit — it's only ever asked whether text fits the *rect*,
never whether the rect is still on the page.

**Fix:** `fit_placements_to_page()`, a post-pass run after all of a page's
paragraphs are placed and before redaction. Two stages, cheapest first: (1)
shrink inter-paragraph gaps, never below `MIN_PARA_GAP` (2.0pt); (2) if that
isn't enough, scale every *flowing* paragraph's font down by one uniform
factor (down to `MIN_FIT_SCALE` = 0.72) and re-measure, so the page stays
visually consistent instead of one arbitrary paragraph shrinking. Pinned
placements (folios, running heads — see Round 2) are left untouched, since
they're page-anchored, not flow-anchored. If even the minimum scale won't
fit, the text is laid out clipped at that scale and a warning is reported
via the existing `progress_callback` — clipped-but-flagged beats
silently-dropped.

This required switching `placements` from `(rect, text, size, single_line)`
tuples to `{"rect", "text", "size", "single_line", "pinned"}` dicts
throughout — both branches of `place()`, the redaction-bbox extension, and
the final `fit_and_insert` loop.

**Verified:** built a realistic fixture (word-spaced fake paragraphs, not a
single unbroken token — an early test attempt using one giant token
produced misleading near-zero measured heights, since unbroken text doesn't
wrap the way real translated prose does) whose four paragraphs would
naturally stack to y1=1076 on a 640pt-tall page — 436pt past the bottom.
After the fix, all four fit within y1=622 (under the 640pt/18pt-margin
limit) via a uniform 0.76× font scale, with **all 800 words of source text
still present on the page** — none silently dropped.

### 2. High — footnote/reference paragraphs render at the superscript marker's size — Fixed + verified

`split_page_into_paragraphs` took a paragraph's font size from
`para_lines[0]["spans"][0]["size"]` — the *first span of the first line*. A
footnote or reference entry opens with its superscript number, so the first
span is the ~6.5pt marker, and the whole 9pt entry was rendered at 6.5pt.
This also skewed `page_body_size` (`max` over paragraph sizes), which drives
the small-text/tight-run logic in reformat mode.

**Fix:** use `dominant_size(para_lines)` — the size covering the most
*characters* in the paragraph — instead of the first span's size.

**Verified:** a synthetic footnote paragraph (6.5pt marker span + 9pt body
text span on one line) now measures `size=9.0`, not `6.5`.

### 3. High — consecutive footnote entries merge into one run-on paragraph — Fixed + verified

Footnote/reference entries sit flush at the body margin, same size, one
after another with normal line spacing — none of the three existing
new-paragraph signals (indent, size jump, large gap) fires between them, so
consecutive entries were concatenated and translated as a single blob (and,
worse, if translation dropped the markers, `preserve_footnote_markers`
would append *both* `[1] [2]` at the end of the merged blob — every
footnote in the block lost its individual number).

**Fix:** added `starts_with_marker(line)`, which checks whether a line's
first non-blank span is a footnote marker (via `span_is_footnote_marker`,
see Finding 6 below), and added it to the paragraph-break disjunction,
guarded by the existing `not same_row` check so the wide-word-gap case
(same visual line split into several PyMuPDF "line" dicts) is unaffected.

**Verified:** a synthetic block with two consecutive footnote entries and a
wrapped continuation line now splits into two separate paragraphs
(`'[1] Vgl. Horkheimer, S. 105-109.'`, `'[2] Adorno, Minima Moralia, S. 12.'`)
instead of one merged blob, with the continuation line correctly staying
attached to entry `[1]` (it doesn't start with a marker, so it isn't itself
treated as a new paragraph).

### 4. Medium — a literal asterisk italicizes everything up to the next one — Fixed + verified

`text_to_html` paired asterisks purely positionally (`r"\*(.+?)\*"`). A
literal `*` in the source — German academic prose uses `* 1903` for a birth
date, and plain arithmetic (`2 * 4`) also occurs — opened a bogus italic run
extending to the next asterisk anywhere in the paragraph.

**Fix, two parts:**
- `line_text_marking` now parks any literal `*` in the source on a
  private-use Unicode sentinel (`ASTERISK_SENTINEL`) before building the
  paragraph string. The sentinel survives translation as an opaque
  character (it's not meaningful text the model would touch) and is
  restored to a literal `*` at render time.
- `ASTERISK_RUN_RE` was tightened to real markdown-emphasis rules — the
  opening `*` must be followed by non-space, the closing `*` preceded by
  non-space, and neither may sit adjacent to a word character or another
  asterisk (`r"(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])"`) — so a bare
  `*` used as punctuation can never open a run in the first place.
- `text_to_html`'s escape step also now drops any *unmatched* leftover `*`
  locally (per chunk) instead of the old whole-string parity check, which
  meant one dropped asterisk anywhere killed italics for the *entire*
  paragraph.

**Verified** (with the sentinel substitution applied, matching real
pipeline usage):
```
'Adorno ( 1903) und *Minima Moralia*' -> 'Adorno (* 1903) und <i>Minima Moralia</i>'
'*Kritische* und *Theorie*'                 -> '<i>Kritische</i> und <i>Theorie</i>'
'**Dialektik**'                             -> '<i>Dialektik</i>'
'*Schulte-**Sasse*'                         -> 'Schulte-<i>Sasse</i>'
```

### 5. Medium — reformat mode inserted a space at every hyphen break — Fixed + verified

The `dehyphenate=False` path (used in `--reformat-only` mode, since
`insert_htmlbox` never hyphenates the way German typesetting does, so any
`-` there is always a real character) fell through to the plain
space-joining branch, producing `"Schulte- Sasse"` and `"S. 105- 109."`
instead of `"Schulte-Sasse"` / `"S. 105-109."` — a different corruption
than the one Round 1 had fixed, on the pipeline's own re-rendered output.

**Fix:** hoisted the hyphen-at-break test (`at_break_hyphen`) out from under
the `dehyphenate` flag, and gave reformat mode ( `not dehyphenate`) its own
branch: keep the hyphen, drop the space, merging the two runs directly
(handling the italic-boundary case specially so two adjacent `*` don't
produce an invalid `**`). The suspension check (`_SUSPENSION_RE`, for
"Sozial-" + "und") is checked *before* the reformat-mode branch in both
modes, so a suspended compound never loses its space in either mode.

**Verified**, 8 cases across both `dehyphenate` values:
```
(['Schulte-', 'Sasse, Literarische'], False) -> 'Schulte-Sasse, Literarische'
(['Auf-', 'klaerung'], False)                -> 'Auf-klaerung'
(['S. 105-', '109.'], False)                 -> 'S. 105-109.'
(['Zur Sozial-', 'und Wirtschaftsgeschichte.'], True)  -> 'Zur Sozial- und Wirtschaftsgeschichte.'
(['Zur Sozial-', 'und Wirtschaftsgeschichte.'], False) -> 'Zur Sozial- und Wirtschaftsgeschichte.'
(['vgl. S. 105-', '109 dazu.'], True)        -> 'vgl. S. 105-109 dazu.'
(['Schulte-', 'Sasse hat recht.'], True)     -> 'Schulte-Sasse hat recht.'
(['das *Kapi-', 'tal* von Marx'], True)      -> 'das *Kapital* von Marx'
```

### 6. Medium — Unicode superscript markers become unrecoverable — Fixed + verified

`str.isdigit()` returns `True` for `¹` (superscript one, Unicode category
No), so a superscript-glyph footnote marker passed the old marker test and
was emitted verbatim as `[¹]`. But `FOOTNOTE_MARKER_RE` is `\[(\d{1,3})\]`,
and `\d` matches only category Nd — so `[¹]` could never be matched again:
not counted by `preserve_footnote_markers`, not restored if translation
dropped it, and not recognized by the near-empty-paragraph guard (so a lone
`[¹]` was sent to the model as a translation request — exactly what that
guard exists to prevent). Separately, a size-only marker test misses
superscript glyphs set at full body size, since the superscripting there is
in the character, not the font size.

**Fix:** `as_marker_digits(text)` normalizes Unicode superscript digits
(`⁰¹²³⁴⁵⁶⁷⁸⁹`) to ASCII via `str.translate`, and accepts a span as a marker
if it's all-digit-after-normalization *and either* smaller than body text
*or* written with superscript glyphs to begin with (`span["text"].strip()
!= digits` catches the latter). This replaced the old inline
`size < dominant * 0.8 and text.isdigit()` check both in
`line_text_marking` and in `body_size_of_line`'s footnote-marker exclusion,
now unified as `span_is_footnote_marker()` — which Finding 3's
`starts_with_marker()` also depends on.

**Verified:** `as_marker_digits('¹')` → `'1'`, and `FOOTNOTE_MARKER_RE`
correctly recovers `[1]` from a string built with the normalized marker.

### 7. Low — single-line slack pulls headings off the body margin — Fixed + verified

`place()`'s width-slack for single-line paragraphs (added originally so a
short heading wouldn't unexpectedly wrap when translation made it a touch
wider) split evenly onto both sides (`x0 -= pad/2; x1 += pad/2`). But these
boxes are left-aligned, so widening leftward moved the *text itself* left
of the body margin — measured: a 14pt heading drifted 10.5pt left of the
column it should align with, and after Finding 2's fix, short footnote
entries (now correctly single-line) drifted 6.7pt too.

**Fix:** add all the slack on the right only (`x1 = min(x1 + para["size"] *
1.5, page.rect.x1 - 2)`), leaving `x0` untouched.

**Verified:** a synthetic heading + body paragraph both now render at
`x0=60.0` — exactly aligned, no drift.

### 8. Low — the `add_eos_token` guard can't actually fire — Fixed + verified

The existing code registered `<end_of_turn>` as an EOS token via mlx-lm's
`add_eos_token()`, with a comment claiming it "raises here, loudly" for a
tokenizer where the token doesn't resolve. It doesn't: mlx-lm's
implementation calls HF's `convert_tokens_to_ids`, which returns
**`unk_token_id`**, not `None`, for an unrecognized token — so
`add_eos_token`'s own `if token_id is None: raise` never fires, and the
*unrelated* unk-token id gets silently registered as an EOS token instead.
Harmless with the pinned model (`<end_of_turn>` is genuinely in Gemma's
vocabulary), but the safety net itself was fictional.

**Fix:** call `tokenizer.convert_tokens_to_ids("<end_of_turn>")` directly
first, and explicitly check both `eot is None` and `eot == unk_token_id`
before ever calling `add_eos_token` — so a tokenizer where this resolves to
the unk token now raises a clear `RuntimeError` instead of silently mis-
registering.

**Verified:** ran a real translation through the actual pinned model after
the change — clean, correctly-terminated generation, `'The dog is running
fast.'` for `'Der Hund laeuft schnell.'`, no runaway generation to
`max_tokens`.

### 9. Low — Colab notebook loaded a 12B model while documenting 4B — Already resolved

`colab_translate.ipynb` briefly diverged to `google/translategemma-12b-it`
(commits `f3ebdbf`/`1a705b5`/`c970e90`, chasing a Colab system-RAM OOM
during the 12B download) while its own title, intro markdown, and the
repo's `DEFAULT_MODEL` all still said 4B. This has since been reverted
(`9fc66ab`): `MODEL_ID` is back to `google/translategemma-4b-it`, matching
the documented model everywhere. No action needed here beyond noting it.

The audit's second note on this notebook — that `AutoModelForCausalLM` +
`AutoTokenizer` isn't the model card's documented loader for a Gemma-3-
derived checkpoint (the tagged loader is `Gemma3ForConditionalGeneration` +
`AutoProcessor`, with `Gemma3ForCausalLM` for a text-only load that omits
the vision tower) — has **not** been separately verified or applied. Flagged
here as still open if the notebook needs revisiting; the audit itself
couldn't run this (no GPU on that machine) and called it "the documented
path to verify rather than a tested fix."

### 10. Low — watcher never retranslates a replaced file — Fixed + verified

`scripts/auto_translate_pdf.py` keyed its `done` set on the relative path
alone. Dropping a corrected scan under the same filename was skipped
forever, since `out.exists()` (the stale previous `_en.pdf`) is what
confirmed "already done."

**Fix:** `file_key(p)` folds size and mtime into the key
(`"name.pdf:43038:1787731189"`), used everywhere `done`/`attempts`/`failed`
are checked or updated. An old state file degrades gracefully — its
path-only entries simply don't match the new key format, so those files
get retranslated once rather than erroring.

**Verified:** ran the watcher against a file already marked done under the
old path-only key; it correctly re-evaluated the file under its new
`path:size:mtime` key (`1-95-43-56.pdf:43038:1787731189`), found the
existing output, and marked it done under the new key -- confirming both
the new key format works and the degrade-gracefully behavior holds.

---

## What's still open

- **Finding 9's loader concern** (Colab notebook's `AutoModelForCausalLM`
  vs. the model card's documented `Gemma3ForConditionalGeneration` +
  `AutoProcessor`) — not verified; no GPU available to test it here either.
- **Minor/non-blocking items from Round 2's follow-up** that were listed but
  not actioned: adjacent italic runs merging cosmetically
  (`'*Marx* *Engels*'` → one combined `<i>` run instead of two — renders
  identically, purely cosmetic), `wait_until_stable`'s first-poll always
  costing ~2s even for a long-stationary file, a 0-byte file polling for
  the full 120s timeout on every trigger without burning an attempt, a
  copy slower than that same 120s timeout stranding a file with nothing to
  retrigger it, the FATAL-import path not writing to the CSV log, a
  `.part` file surviving a hard kill as harmless clutter, an `# noqa: E402`
  documentation nit for the deliberately-ordered `hf_transfer` env var, and
  `output_path` preserving the original `.pdf`/`.PDF` case.
- **The two "worth adding regardless" items from Round 1**, still not
  built: a golden-file regression test (`skip_translation=True` gives a
  fully deterministic path through extraction/reflow/rendering with no
  model load — several of the bugs above would have been caught by one
  such fixture) and a `--check` mode reporting paragraph count/detected
  body size/modal margin/near-empty-paragraph count without translating.
All 10 of Round 3's findings are now applied. Unlike Rounds 1 and 2 (commits
`7a91255` and `4090c49`), Round 3's commit hash isn't filled in above since
it's written before that commit is made — check `git log` for the actual
hash if this file is read after the fact.
