# auto-translate-pdf -- audit & fix history

For what this project is and how the pipeline works, see [README.md](README.md)
-- this file used to duplicate that description (and had already started
drifting from it) and now only carries the audit-round record, which
README.md doesn't.

Four rounds of external audit have been run against this pipeline since the
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
the vision tower) — has **not** been separately verified or applied, and
the audit itself couldn't run this (no GPU on that machine) and called it
"the documented path to verify rather than a tested fix." **Won't fix**:
the Colab notebook isn't planned for further work, so this is closed
without action rather than left open indefinitely.

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

All 10 of Round 3's findings are applied in commit `b896273` "Fix all 10
findings from third-party audit round 3".

---

## Round 2 leftovers — minor/non-blocking items, now actioned

These eight items were listed after Round 2's follow-up but not acted on at
the time (see "What's still open" as it read through Round 3). None were
regressions or user-facing correctness bugs on their own — that's why they
sat for two rounds — but each was cheap to fix once picked back up.

1. **Adjacent italic runs merged cosmetically.** `text_to_html`'s pass to
   drop "empty" runs left over from `**bold**` normalization
   (`re.sub(r"\*(\s*)\*", r"\1", text)`) couldn't tell an empty run from the
   ordinary word-gap between two separate real runs — `'*Marx* *Engels*'`
   has a `"* *"` sequence at the boundary between them, identical in shape
   to an emptied-out `"**  **"`. The regex collapsed both, merging
   `'*Marx* *Engels*'` into one `<i>Marx Engels</i>` run.
   **Fix:** removed the pass entirely. It turned out to be unnecessary: an
   empty/whitespace-only run can never match `ASTERISK_RUN_RE` in the first
   place (its opening `*` must be followed by a non-space character), so a
   leftover `"*  *"` already falls through to `esc()`'s unmatched-delimiter
   drop with no extra step needed.
   **Verified:** `text_to_html('*Marx* *Engels*')` → `'<i>Marx</i>
   <i>Engels</i>'` (previously one merged run); `'**Dialektik**'` and the
   `'das *Kapital* von Marx'` mid-sentence case are both unaffected;
   `'a **  ** b'` (the case the old pass targeted) still renders as plain
   `'a    b'` with no empty `<i>` tag.

2. **`wait_until_stable`'s first poll always cost ~2s**, even for a file
   that had been sitting untouched for hours. **Fix:** a fast path checks
   the file's mtime before polling at all — if it's already older than
   `checks * interval`, the file is stable by definition and the function
   returns immediately. **Verified:** a file backdated 30s returned `True`
   in 0.00s (vs. a guaranteed ~2s before).

3. **A 0-byte file polled for the full 120s timeout on every single
   trigger**, since size 0 can never satisfy the "unchanged and > 0"
   stability check. **Fix:** added a separate, much shorter
   `zero_byte_timeout` (5s default) that a stuck-at-zero file hits and
   bails out on well before the main timeout. **Verified:** a permanently
   0-byte file now returns `False` in ~2s instead of 120s.

4. **A copy slower than the 120s stability timeout stranded its file
   indefinitely** — a Folder Action fires only on "item added," so once
   `wait_until_stable` gives up, nothing triggers this script again for
   that file until some *other* file is dropped in the folder. **Fix:**
   `schedule_retry()` spawns a detached, self-relaunching background
   process (`sleep 150 && exec python3 auto_translate_pdf.py`) whenever a
   file is still copying at the end of a run, deduplicated via a
   `.auto_translate_retry_pending` marker file so a busy folder doesn't
   spawn a pile of them; the marker clears once a run finds nothing left
   waiting on stability.

5. **The FATAL-import path didn't write to the CSV log.** If
   `from translate_pdf import ...` failed (e.g. no serif font pair on this
   machine), the failure went to stdout and the progress log but never to
   `translate_log.csv` — the one place a user would think to check.
   **Fix:** added the missing `log_result(...)` call on that path.

6. **A `.part` file survives a hard kill as harmless-but-permanent
   clutter.** `process_pdf` already saves to `<output>.part` and renames on
   success specifically so a *normal* failure never leaves a corrupt file
   behind, but a hard kill (force-quit, `kill -9`, a crashed process) skips
   the rename and leaves the `.part` file sitting in the folder forever.
   **Fix:** `cleanup_stale_part_files()` removes any leftover `*.part` file
   at the start of every run — safe because it only runs while holding
   `LOCK_FILE`, so any `.part` file found there cannot belong to a
   still-running translation.

7. **An `# noqa: E402` documentation nit.** `import pymupdf as fitz` sits
   after the `HF_XET_HIGH_PERFORMANCE` `os.environ.setdefault()` call
   deliberately (it must run before `huggingface_hub` is imported), which
   is a textbook E402 lint flag with no comment explaining why it's there
   on purpose. **Fix:** added the `# noqa: E402` plus a comment pointing at
   the ordering requirement.

8. **`output_path` and the original file's `.pdf`/`.PDF` case.** Verified
   this was already correct — `p.with_name(f"{p.stem}{EN_SUFFIX}{p.suffix}")`
   uses `p.suffix`, which preserves the source extension's case verbatim
   (`Bericht.PDF` → `Bericht_en.PDF`). The actual gap was the module
   docstring, which described the output as always `<name>_en.pdf`
   (implying a fixed lowercase extension); corrected it to describe the
   real, case-preserving behavior.

All eight are in `scripts/auto_translate_pdf.py` except #1 and #7, which are
in `translate_pdf.py`.

---

## The two Round 1 "worth adding regardless" items, now built

1. **A golden-file regression test** (`tests/test_golden.py` +
   `tests/fixtures.py`). Builds a synthetic fixture PDF exercising the
   trickier corners of the pipeline in one page — indentation-based
   paragraph splitting, a real italic run sitting next to a literal
   asterisk, two back-to-back footnote entries distinguishable only by
   their leading marker (Finding 3), one of them hyphenated across a line
   break (Finding 5), and a footer folio (Round 2's pinning fix) — and runs
   it through `process_pdf` with `load_model`/`translate` monkeypatched to
   stubs (an identity function standing in for translation). No model
   download, no GPU/Apple Silicon dependency, no non-determinism from an
   actual LLM — the same technique Round 3's own audit used to build its
   test harness. Assertions are deliberately split across two views: what
   was *sent to `translate()`* (proves the source-side paragraph-splitting/
   marker/hyphenation decisions, captured via the stub) vs. what actually
   *ended up in the rendered output* (proves italics, the literal asterisk,
   and the folio's position survived rendering) — re-parsing the rendered
   output to check paragraph-splitting doesn't work, because by then
   footnote markers are just plain same-size inline text and the marker-
   based split signal has nothing left to key off, which is expected
   output-side behavior, not a regression.

   Sanity-checked the harness actually catches regressions by temporarily
   disabling Finding 3's `starts_with_marker` check and re-running: the
   "sent as separate translate() calls" assertion correctly failed, showing
   both footnote entries merged into one call, exactly the Finding 3 bug.
   Restored before landing.

   One fixture-construction wrinkle worth noting for anyone extending this
   test: the footnote lines are placed with low-level `page.insert_text()`
   rather than `insert_htmlbox()`, because `insert_htmlbox` itself silently
   rewrites a literal `"-"` at a line break into an invisible soft hyphen
   (U+00AD) — a real rendering quirk (already handled via the `"\xad"` ->
   `"-"` normalization in `split_page_into_paragraphs`, for when this
   pipeline re-extracts its *own* prior output), but one that would corrupt
   a fixture meant to simulate a genuine source PDF with a real ASCII
   hyphen. `insert_text()` places literal glyphs with no such rewrite.

2. **A `--check` mode**: `translate_pdf.py input.pdf --check` reports, per
   page, the paragraph count, detected body font size, modal left margin,
   and near-empty-paragraph count — without loading the model, translating,
   or writing any output. `output` is no longer a required positional
   argument when `--check` is passed. Implemented via a new
   `modal_left_margin()` helper factored out of
   `split_page_into_paragraphs` (so the diagnostic and the real
   paragraph-splitting logic can never quietly drift apart) and a new
   `check_pdf()` function that reuses `split_page_into_paragraphs` directly.
   **Verified** against the golden fixture: `1 page: 6 paragraph(s), body
   size 14.0, modal left margin 72.0, 1 near-empty` — matching the fixture's
   known contents (heading, 2 body paragraphs, 2 footnote entries, 1 folio;
   the folio is the near-empty one).

All findings from Rounds 1–3 are fixed and verified, the Round 2 leftovers
above are fixed and verified, and both Round 1 "worth adding regardless"
items are built. Finding 9's loader concern (above) is the one item closed
as won't-fix rather than fixed.

---

## Round 4 — fresh audit against `fabee59` (22 findings)

Commit audited: `fabee59` ("Fix Round 2 leftovers, add golden test + --check
mode"). This audit cloned the repo on Linux x86, installed `pymupdf` alone,
ran `tests/test_golden.py` (all 11 checks passed), then wrote targeted repro
scripts against the real pipeline with `load_model`/`translate` stubbed.
Findings marked **CONFIRMED** in the audit had an executed repro; findings
marked **BY INSPECTION** were code readings that needed either a real model
or macOS to execute (the Folder Action path, the mlx-lm generation path).

Fixed in the audit's own suggested order: **1** (report to stderr, makes
everything else visible while working on it) → **15 + 16** (split
requirements, add CI) → **2, 3, 4** (the layout-correctness cluster) →
**5** + `tests/test_units.py` (**17**) → **7, 8** (input validation, column
detection) → **6, 9, 10** (the model-call cluster) → **11, 12, 14** (watcher
robustness) → everything else (13, 18–22).

### 1. P0 — the overflow/clipping warning never reached anyone on the CLI — Fixed + verified

`report()` was a no-op unless a `progress_callback` was passed, and the CLI
(`translate_pdf.py in.pdf out.pdf`) passes none — so `fit_placements_to_page`'s
overflow warning and the skipped-near-empty-paragraph notices were computed,
formatted, and thrown away on every invocation except the Folder Action
watcher's, defeating Round 3 Finding 1's whole point on the standard CLI path.

**Fix:** `report()` now always prints to stderr in addition to calling the
callback when one is given; the now-duplicated bare `print(..., file=sys.stderr)`
calls in the page loop were folded into `report()` calls instead, so each
event is emitted exactly once. A genuine (not just rescaled) overflow now
reports with a stable `OVERFLOW:` prefix a script can grep for, and
`fit_placements_to_page` returns whether content still overflowed, threading
a real signal out of what used to be a fire-and-forget call.

**Verified:** re-ran the audit's own repro (a fixture expanded 14×, no
`progress_callback` passed) — the warning now appears on stderr.

### 2. P0 — reflowed body text could collide with the pinned folio — Fixed + verified

`fit_placements_to_page`'s bottom limit was a fixed page-box margin with no
knowledge of where pinned placements (folios, running heads) actually sit --
its own docstring already named this exact failure ("the overflow also lands
on top of any pinned folio on its way down"), but the implementation only
guarded against running off the media box.

**Fix:** the limit is now clamped to `min(page-box limit, nearest pinned
item below the body) - MIN_PARA_GAP`; symmetrically, the flowing content's
top is pushed below any pinned item sitting at/above its natural start (a
header running head).

**Verified:** a 3-paragraph body on a 400pt-tall page, translated text
expanded 12× (`tests/test_layout.py`), correctly rescales to 0.84× and
produces zero overlap between any flowing line's rect and the pinned folio's
rect; the fit outcome is reported via Finding 1's stderr path.

### 3. P0 — only bare-digit paragraphs were page-anchored; textual running heads/footers rode the reflow — Fixed + verified

`pinned=True` was reachable from exactly one place: the branch matching a
paragraph against the bare-page-number regex. A textual running head or
footer ("Kapitel 3 - Einleitung", the norm in academic German typesetting)
was ordinary body prose to the pipeline and inherited the body's accumulated
shift -- the exact Round 2 regression, for a class of furniture that fix
never covered.

**Fix:** `is_page_furniture(para, text)` decides pinning on geometry (in the
header/footer 12% band, single line, under 80 characters) for *every*
paragraph, not just digit-only ones; the bare-number regex branch now only
decides "don't send this to the translator," nothing about layout.

**Verified:** a page with a textual header ("Kapitel 3 - Einleitung")
authored at y=40 and a body paragraph below it, translated text unchanged in
length -- the header stays within 10pt of y=40 in the output instead of
drifting with the body.

### 4. P1 — first-line indent was dropped, so indent-separated paragraphs merged visually — Fixed + verified

`split_page_into_paragraphs` uses first-line indentation as its primary
paragraph-detection signal, but the indent was only ever consumed, never
reproduced -- `paragraph_html` emitted no `text-indent`, and `place()` used
the paragraph's bbox union (the flush margin), not the indented first line.
For the layout convention this splitter is built around (no blank line
between paragraphs, indent only), this removed the source's *only*
paragraph separator from the output: two indent-separated paragraphs read
as one continuous block in the translated PDF.

**Fix:** `split_page_into_paragraphs` records `indent = para_lines[0].x0 -
bbox.x0` per paragraph (0 for single-line paragraphs, which are headings/
bylines, not indented body prose); threaded through `paragraph_html`,
`measure_height`, `fit_and_insert`, and every `placements` dict.

**Verified:** two indent-separated 2-line German paragraphs (first line at
x0=90, continuation at x0=72) -- after translation (identity stub), both
paragraphs' first lines render at x0=90.0 in the output, distinguishable
from their own continuation lines at x0=72.0.

### 5. P1 — `join_paragraph_lines` lost italics across a hyphenated line break in the proper-noun/page-range branch — Fixed + verified

Three of the join's four branches merge the two runs when a hyphen break
falls inside an italic run; the final `else` branch (proper nouns, page
ranges: "Schulte-" + "Sasse") didn't, producing `*Schulte-**Sasse*`, which
`text_to_html`'s `**`->`*` bold-normalization then closed one character
early, silently dropping italics from the first half: `Schulte-<i>Sasse</i>`.

**Fix:** added the same `if tail and opens_italic: merge` guard the sibling
branches already had.

**Verified**, both directly (`join_paragraph_lines(['*Schulte-*', '*Sasse*'],
True)` → `'*Schulte-Sasse*'`, and the same for `'*Titel-*'`/`'*Fortsetzung*'`)
and via `tests/test_units.py`'s full branch × italic × dehyphenate matrix,
which would have caught this directly without any PDF rendering at all.

### 6. P1 — `max_tokens=1024` could silently truncate long paragraphs — Fixed (by inspection; no GPU here to run the real model)

A single fixed cap for every paragraph, with nothing checking whether
generation stopped at `<end_of_turn>` or ran out of budget -- a dense
academic paragraph or footnote block (2500-3500 chars is normal) can exceed
1024 output tokens, and the cut-off translation would be inserted as if
complete; `preserve_footnote_markers` would then append the truncated
tail's markers, making it look *more* plausible.

**Fix:** `max_tokens` is now sized off the source (`max(256, min(4096,
int(n_src * 2.5) + 64))`, `n_src` from `tokenizer.encode`), and a heuristic
truncation check reports a warning when the source ended in sentence-final
punctuation, the output didn't, and the output is already using most of the
implied character budget. Not a hard guarantee (mlx_lm.generate's plain-
string return exposes no finish reason to check directly), but strictly
better than the old fixed cap with no check at all.

### 7. P1 — no input validation before the ~2GB model load — Fixed + verified

An encrypted PDF surfaced as a cryptic `ValueError: document closed or
encrypted` on the first `page.get_text()` -- *after* the full model load. An
image-only scan silently extracted zero paragraphs, redacted nothing, and
saved the untouched German original under an `_en` name with `status=ok` in
the watcher's log.

**Fix:** `preflight(doc, page_indices, in_path, report)` runs before
`load_model`, raising a new `UnsupportedInputError` (a `ValueError`
subclass) for a password-protected PDF, zero pages, or zero extractable
characters, and reporting a warning for suspiciously little text. The
watcher catches `UnsupportedInputError` specifically and routes it straight
to `failed` instead of burning `MAX_ATTEMPTS` retries that would fail
identically every time.

**Verified:** a password-protected PDF and a text-free (image-only) PDF each
raise the expected `UnsupportedInputError` with an actionable message
(`... run OCR (e.g. ocrmypdf) first`) before any model-loading code runs.

### 8. P1 — multi-column pages silently interleaved into one scrambled paragraph — Fixed + verified (detection; full column support out of scope)

Lines are sorted page-wide by `(y, x)`; on a two-column page this zips the
columns together row by row into one fluent-sounding, confidently wrong
paragraph, and `--check`'s own "1 paragraph" reads as healthy -- a false
all-clear from the exact diagnostic meant to catch this.

**Fix (detection):** `detect_columns(lines, page_width)` clusters line x0
values by gaps larger than 15% of the page width (deliberately large --
ordinary indented-paragraph prose already produces two x0 populations 12-24pt
apart, and a small threshold would flag every such document as
"multi-column"), then flags 2+ clusters if each covers more than 25% of the
page's lines. `check_pdf` reports a warning; `process_pdf` refuses with
`UnsupportedInputError` unless `force=True` / `--force` is passed. Full
column-aware reflow (partition into bands, run the splitter per band) is the
larger fix the audit itself called "the single largest capability gap" and
is not attempted here.

**Verified:** a synthetic two-column page (`tests/test_units.py`) is
detected as 2 columns; an ordinary indented single-column fixture stays at
1; `process_pdf` raises without `--force` and proceeds (with a warning)
with it.

### 9. P1 — translation was non-deterministic by default and unseeded — Fixed (by inspection; no GPU here to run the real model)

`temp=0.3` bought nothing for a one-right-answer task and cost run-to-run
reproducibility -- directly undercutting reformat-only mode's own stated
rationale ("re-translating would introduce fresh non-determinism"). No RNG
seed was set either.

**Fix:** `DEFAULT_TEMP = 0.0`, exposed as `--temp` on the CLI;
`load_model()` seeds `mx.random` explicitly (`DEFAULT_SEED = 0`, `--seed`),
so even a non-zero `--temp` stays repeatable run to run.

### 10. P2 — no sanity check on what the model returns — Fixed + verified

The near-empty guard prevents *sending* a degenerate request but nothing
inspected the *response* -- an echoed source, a conversational refusal
("Please provide the German text..."), or a wildly truncated/runaway output
would be inserted into the PDF verbatim.

**Fix:** `bad_translation_reason(text, translated)` flags an identical
echo (paragraphs over 40 chars), an output under 35% or over 300% of the
source length, or a conversational-refusal shape via regex. `process_pdf`
reports the reason and, when `temp != 0.0` (a deterministic retry at the
same settings would just reproduce the same output), retries once at
`temp=0`.

**Verified** via `tests/test_units.py`'s direct cases for all four shapes,
plus the golden test's identity stub correctly triggering the echo case
(expected, documented in the stub's own comment -- not a bug).

### 11. P2 — the watcher crashed if a pending file disappeared mid-run — Fixed + verified

`file_key(p)`'s unguarded `p.stat()` raised `FileNotFoundError` for a file
moved or deleted between the initial `rglob()` and the pending-list
comprehension -- a normal thing to happen in a watched drop folder -- taking
down the whole run, including every file queued behind it. `file_key` was
also computed twice per file for no reason.

**Fix:** `file_key` catches `OSError` and returns `None`; the pending-list
construction skips a `None` key and stores `(path, key)` tuples so the
consuming loop reuses the already-computed key instead of recomputing it.

### 12. P2 — `schedule_retry` built a shell command with Python `repr`, not shell quoting — Fixed + verified

`f"... {sys.executable!r} {script!r}"` inside a `/bin/sh -c` string used
Python quoting, not POSIX quoting -- a repo path containing a single quote
produced a broken, in-principle-injectable command line.

**Fix:** dropped the shell entirely; the child does `time.sleep()` then
`runpy.run_path()` directly, so there's no quoting to get wrong.

**Verified:** a script at a path containing a literal single quote
(`/tmp/Anna's Docs/dummy_script.py`) now runs correctly end to end via the
new `subprocess.Popen` argv list, with no shell involved.

### 13. P2 — the literal-asterisk sentinel traveled through the LLM, and model-emitted asterisks were deleted — Fixed + verified (both halves)

`ASTERISK_SENTINEL` (U+F8FF, Apple's corporate private-use codepoint) was
sent through `translate()` verbatim, with nothing guaranteeing a model
tokenizes or reproduces an obscure PUA character faithfully. Separately,
`text_to_html`'s `esc()` dropped *every* unmatched `*` unconditionally --
so a footnote star, birth-date asterisk, or markdown bullet the model itself
emitted in its *output* vanished silently.

**Fix:** `strip_sentinel_for_model`/`restore_sentinel_from_model` swap the
sentinel for a plainer placeholder (`XASTERISKX`) just for the
`translate()` round trip, restoring immediately on return -- the sentinel
itself never reaches the model. `esc()` no longer strips a leftover `*`;
it's kept as a literal character (html.escape doesn't treat `*` as special),
so deletion is no longer the default for the model's own asterisks.

**Verified:** `strip_sentinel_for_model`/`restore_sentinel_from_model`
round-trip exactly (`tests/test_units.py`); `text_to_html("2 * 4 = 8")` now
renders the arithmetic literally instead of deleting the asterisk.

### 14. P2 — watcher state/temp files lived inside the watched folder — Fixed + verified

A macOS Folder Action fires on *item added*, so the watcher's own
bookkeeping files (`.auto_translate_state.json`, the progress log, the
retry marker) re-triggered the very Folder Action that runs the script.
`.translate_progress.log` also grew without bound.

**Fix:** state, progress log, retry marker, and the lock file all moved to
`~/Library/Application Support/auto_translate_pdf/`; only the append-only,
human-facing `translate_log.csv` stays in the watched folder. The progress
log truncates past 5MB. `translate_pdf.py`'s `.part` temp file now has a
leading dot on the *filename* (`.Bericht_en.pdf.part`, not
`Bericht_en.pdf.part`), so it doesn't surface as a visible new item either.

**Verified:** `LOCK_FILE`/`STATE_FILE`/`PROGRESS_FILE`/`RETRY_MARKER` all
resolve under the new Application Support subdirectory; a real
`process_pdf` run produces a `.` -prefixed `.part` temp file.

### 15. P2 — `mlx-lm` was a hard install requirement for Apple-Silicon-only code — Fixed + verified

`requirements.txt` pinned `mlx-lm`, which only installs on Apple Silicon --
but `--check`, `--reformat-only`, and `tests/test_golden.py` need only
PyMuPDF, and couldn't be run at all on Linux, Intel Mac, or CI.

**Fix:** split into `requirements.txt` (just `pymupdf`) and
`requirements-mlx.txt` (pulls in the base file plus `mlx-lm` and
`huggingface_hub`). No code change needed -- `mlx_lm` was already imported
lazily inside `load_model`/`translate`, never at module scope.

**Verified:** `python -m py_compile translate_pdf.py` and the full test
suite (`test_golden.py`, `test_units.py`, `test_layout.py`) all run with
only `pymupdf` installed.

### 16. P2 — no CI — Fixed

`.github/workflows/test.yml` runs `tests/test_golden.py` and
`tests/test_units.py` on stock `ubuntu-latest` with `fonts-liberation`
installed, using just `requirements.txt` (see Finding 15). Both test files
already exit non-zero on failure, so no extra wiring was needed.

### 17. P2 — test-coverage gaps — Fixed (partial: units + targeted layout cases; full multi-fixture matrix not attempted)

The golden test covered one page, one layout, the happy path. Added:

- `tests/test_units.py`: plain-assert unit tests over the pure functions
  (`join_paragraph_lines` across all four branches × italic × dehyphenate,
  `text_to_html`, `as_marker_digits`, `span_is_footnote_marker`,
  `preserve_footnote_markers`, `bad_translation_reason`, `parse_page_range`,
  `modal_left_margin`, `detect_columns`) -- these would have caught Finding
  5 directly, with no PDF involved at all.
- `tests/test_layout.py`: the overflow/rescale-vs-pinned-folio collision
  case (Findings 1, 2), a textual running head staying pinned (Finding 3),
  multi-page reflow-state-doesn't-leak, and `--reformat-only` end to end.

Not built: a synthetic rotated-page fixture (the audit spot-checked
`/Rotate 90` by hand and found it correct, "an accident of PyMuPDF's
coordinate handling, not something the code reasons about") and a dedicated
multi-column *support* test (only detection is implemented; see Finding 8).

### 18. P2 — `measure_height` was uncached and re-measured on every rescale pass — Fixed + verified

Each call opened a fresh `fitz.Document` and ran a full `insert_htmlbox`
layout; `fit_placements_to_page`'s rescale loop re-measures *every* flowing
paragraph on *every* scale step (up to 8 iterations) -- pure overhead in
reformat-only mode, where there's no model call to dominate it.

**Fix:** `@functools.lru_cache(maxsize=4096)` on `_measure_height_cached`,
keyed on `(round(width, 1), text, round(fontsize, 2), single_line,
round(indent, 1))`; the uncached path also now reuses one lazily-created,
process-lifetime scratch `fitz.Document` (adding/dropping a page per call)
instead of opening a brand new document every time.

**Verified:** 300 repeated measurements of the same paragraph: 1.25s
uncached vs. 0.006s cached -- roughly a 215× speedup on the repeated-key
case the rescale loop and reformat-only mode both hit constantly.

### 19. P2 — `AUDIT_FIXES.md` duplicated `README.md` — Fixed

This file's old "What this is" through "License" sections were a
near-verbatim copy of the README, and had already started drifting from it.
Cut down to the audit-round record alone, with a one-line pointer to
`README.md` at the top for the pipeline description.

### 20. P2 — Colab notebook nits — Fixed (all four)

1. The HF-token markdown cell's leftover *"Claude never sees it"* (confusing
   in a public repo, and inaccurate framing regardless) changed to *"isn't
   written to the repo."*
2. The **"Optional: cache the model on Google Drive"** cell used to be the
   *last* cell in the notebook despite its own comment saying "Run this
   BEFORE section 3's model load" -- moved it to just before section 3, and
   updated the now-accurate comment.
3. `!pip install -q transformers accelerate bitsandbytes pymupdf` was
   unpinned while the local path pins everything -- pinned all four
   (`transformers==4.57.1 accelerate==1.1.1 bitsandbytes==0.45.0
   pymupdf==1.28.2`).
4. `output_path = input_path.rsplit(".", 1)[0] + "_en.pdf"` re-implemented
   `scripts/auto_translate_pdf.py`'s `output_path()` and lost extension
   case -- the exact thing Round 2 leftover #8 fixed locally. Replaced with
   the same `Path.with_name(f"{stem}_en{suffix}")` logic (reimplemented
   rather than imported, since the notebook only clones `translate_pdf.py`,
   not `scripts/`).

**Verified:** the notebook's JSON structure (`nbformat`, cell count, cell
ordering) parses cleanly after all four edits; a stale "see the optional
cell below" reference in the Notes section (pointing at where the Drive-
cache cell used to sit) was caught and fixed too.

### 21. P2 — `--pages` produced a mixed-language PDF with no notice — Fixed + verified

Translating pages 1-5 of a 40-page document silently left pages 6-40
German with nothing on stderr or in the output to say so; `parse_page_range`
also silently discarded an out-of-range segment when other segments in the
same `--pages` spec were valid, hiding a typo (`--pages 1-3,99` on a 5-page
doc quietly became pages 1-3).

**Fix:** the CLI now prints `translating N of M page(s); the remaining K
page(s) are copied through untranslated` up front; `parse_page_range` takes
an optional `warn` callback invoked once per segment that selects no pages,
independent of whether the *combined* result across all segments is
non-empty.

**Verified** via `tests/test_units.py`: `--pages 1-3,99` on a 5-page
document returns `[0, 1, 2]` and fires exactly one warning naming the `99`
segment.

### 22. P2 — assorted smaller items — Fixed (all six)

- `main(argv=None)` now wraps the CLI (importable/testable directly, e.g.
  `main(["in.pdf", "--check"])`, rather than only invocable as a
  subprocess).
- `fitz.open(args.input)` failures now go through `ap.error()` with a
  readable message instead of a raw traceback.
- `doc.metadata["title"]` is now translated too (only the title -- not
  `/Subject`/`/Keywords`, rarely populated and not worth a second
  translation request per document), with the same `bad_translation_reason`
  sanity check (Finding 10) guarding it.
- Known limitations (README) now notes that link/widget annotation rects go
  stale after reflow (a paragraph's rect moves; the annotation pointing at
  it doesn't), and that only the document title is translated.
- The German-specific space-before-punctuation regex
  (`re.sub(r"\s+([.,;:!?])", r"\1", text)`) now has a comment noting the
  assumption and its French-quoted-passage failure mode.
- The watcher's `is_translated_output` false-positive case (a genuinely
  German source file that happens to end in `_en`) now logs at debug level
  behind `AUTO_TRANSLATE_DEBUG=1`, off by default to avoid spamming
  `progress.log` on every run for a file that's silently re-skipped
  forever either way.
- `dominant_size`'s empty-input fallback (`10.0`) and
  `split_page_into_paragraphs`'s empty-spans fallback (`"Times-Roman"`)
  were repeated magic literals at two call sites each -- hoisted to
  `FALLBACK_FONT_SIZE`/`FALLBACK_FONT_NAME`.

---

All 22 of Round 4's findings are applied: 20 fixed and verified, plus 2
fixed by code inspection/reasoning only (6, 9 -- both are in the
mlx-lm/model-generation path, which needs real Apple Silicon hardware to
verify against the actual model; unavailable in this environment). Nothing
from this round was skipped or left as won't-fix.

---

## Post-Round-4 self-review — 2 findings

A self-review pass over the Round 4 diff (no external audit; the code was
re-read fresh looking for bugs introduced by that round's own fixes) turned
up two real issues, both in `translate_pdf.py`.

### A. A single multi-column page aborted the *entire* document — Fixed + verified

Finding 8's fix raised `UnsupportedInputError` from inside the per-page
loop when a page looked multi-column and `--force` wasn't given. But
`doc.save()` only runs after that loop completes normally -- raising
partway through discards every other page's already-finished translation
too. A 200-page book with one two-column index or table-of-contents page
(common) would fail to produce *any* output at all unless `--force` was
passed for the whole document, which then also disables the safety net for
any genuinely-scrambling multi-column body pages elsewhere in the same
file.

**Fix:** a flagged page is now skipped (left untranslated, with a warning)
via `continue` rather than raising -- every other page in the document is
still translated normally. `--force` still means "translate this page
anyway" for a page that reaches this check. Updated the `process_pdf`
docstring, `--force`'s CLI help text, and `check_pdf`'s warning wording to
match ("this page will be left untranslated" rather than "output will be
scrambled").

**Verified:** a 3-page synthetic document (pages 1 and 3 fine single-column
German, page 2 a two-column layout) now saves all 3 pages, with pages 1 and
3 translated and page 2 left in German -- confirmed the test fails against
the old raise-based behavior (an uncaught `UnsupportedInputError` crashes
the whole `process_pdf` call) and passes against the fix, added as a
permanent case in `tests/test_layout.py`.

### B. The "--pages" notice printed in `--check` mode, where nothing is translated or copied — Fixed + verified

Round 4 Finding 21's `translating N of M page(s); the remaining K page(s)
are copied through untranslated` notice was printed unconditionally after
parsing `--pages`, before the `if args.check:` branch -- so `--check
--pages 1-5` on a 10-page document printed a translation notice for a run
that translates nothing at all.

**Fix:** moved the notice after the `--check` early-return, and split its
wording for `--reformat-only` ("reformatting... copied through as-is")
vs. normal translation ("translating... copied through untranslated"),
since reformat-only mode was never translating those pages to begin with.

**Verified:** `translate_pdf.py in.pdf --check --pages 1-5` on a
multi-page document no longer prints the notice; `--reformat-only --pages
1-5` prints the reformat-specific wording.

---

## Round 5 — a level deeper: inside-paragraph fidelity and reformat idempotency (14 findings)

Commit audited: `99b52f5` ("Fix all 22 findings from Round 4 audit").
Method: cloned on Linux x86 with only `requirements.txt`, all three
existing test files run first (all pass), then targeted repro scripts
driving the real `process_pdf` with `load_model`/`translate` stubbed,
comparing source vs. output geometry via `get_text("dict")`. Rounds 1-4 had
been thorough on inter-paragraph spacing, marker plumbing, and watcher
robustness; this round's two biggest findings are one level down from
there: what happens *inside* a paragraph (leading), and what happens when
the pipeline eats its own output repeatedly (reformat idempotency).

Fixed in the audit's own suggested order: **1** (content-corrupting, three
lines) → **8, 9** (one-liners) → **14** (rotated-page fixture, added before
the geometry work below) → **2**, then **3** (leading, then reformat
idempotency -- fixing leading first tightens measure/render agreement,
making the idempotency fix cleaner) → **11** (the tripwire for both) →
**4, 7** (the validation cluster) → **5, 6** (bold/typeface -- documented
rather than implemented, both explicitly offered as legitimate cheaper
fixes) → **10, 12, 13** (robustness and hygiene).

### 1. P0 — a real superscript footnote marker went undetected, corrupting the adjacent number — Fixed + verified

`span_is_footnote_marker()` accepted a marker only if it was smaller than
80% of body size, or written with Unicode superscript *glyphs* (¹²³). A
**real PDF superscript** -- ASCII digits, raised by the typesetter, set at
a typical ~0.83x body size -- falls in the dead zone between those two
checks: 0.83 > 0.8 fails the size test, and plain ASCII digits fail the
glyph test. PyMuPDF already flags this (`flags & 1`, PyMuPDF's superscript
bit) and nothing asked. Result: `"1938" + superscript "1"` fused into
`"19381"` before `preserve_footnote_markers` ever saw it -- the exact
failure `[N]` markers exist to prevent, corrupting a *year* -- and
`starts_with_marker()` (which delegates to the same predicate) couldn't
split consecutive footnote-marker-led entries either, regressing Round 3
Finding 3 for this input class.

**Fix:** added `is_superscript = bool(span.get("flags", 0) & 1)` as a third
accepting condition. Noted but left permissive: this also now matches a
genuine math exponent (`x²`), also digits-only and superscript-flagged --
harmless in practice (near-empty guard + `[N]` round-trip make a
wrongly-marked exponent cosmetic, not corrupting), so not worth the
complexity of a "glued to a word" positional guard.

**Verified:** `span_is_footnote_marker({"text": "1", "size": 9.96, "flags":
5}, 12.0)` (9.96/12.0 = 0.83) now returns `"1"` (`tests/test_units.py`);
end to end, a fixture built with real `insert_htmlbox` `<sup>` tags
(`tests/test_layout.py`) confirms the year stays `"1938"` (not `"19381"`)
and both `[1]`/`[2]` markers survive.

### 2. P1 — the source's line leading was never reproduced — Fixed + verified

`paragraph_html()` emitted `font-size`/`text-align`/`text-indent` but never
`line-height`, so every paragraph rendered at MuPDF's user-agent default of
1.2x font-size regardless of the source's actual leading. Quantified on an
identical fixture varying only in leading, translated with an identity
stub (byte-for-byte unchanged text): 15pt leading on 11pt type (1.36x, an
entirely ordinary academic setting) rendered at a fixed 13.2pt (1.2x), a
paragraph-block-height error of **-10%**; 18pt leading (1.64x) was **-22%**.
A source set *tighter* than 1.2x could even trip a spurious
`fit_placements_to_page` rescale with zero text change.

**Fix:** `split_page_into_paragraphs` now records the median of consecutive
line-start deltas within a paragraph, as a *ratio* to font size (e.g.
1.36), not an absolute point value -- CSS's unitless `line-height` is
itself already relative to whatever font-size applies, so a paragraph
rescaled by `fit_placements_to_page` keeps the same proportions for free
with no extra math on this end. Threaded through `paragraph_html`,
`measure_height` (added to the cache key), `fit_and_insert`, and every
`placements` dict, exactly as Round 4 Finding 4 threaded `indent`. Median,
not mean, so one anomalous gap (a widow line, an accidental double break)
doesn't skew the whole paragraph.

**Verified:** a 6-paragraph fixture at 15pt/11pt leading now round-trips to
output line deltas of 15.0pt exactly (`tests/test_layout.py`), across
12/13.2/15/18pt source leadings tested directly (12.00/13.20/15.00/18.00pt
output, exact); the spurious-rescale case (11pt on 11.5pt leading, 54
lines) no longer reports any rescale/overflow.

### 3. P1 — `--reformat-only` was not idempotent; each pass grew the page — Fixed + verified

Traced to a deeper mechanism than the audit's own initial diagnosis (a
manual `+2` pad on each paragraph's rect): `measure_height`'s returned
height was the *CSS line-box height* (`3000 - spare_height` from
`insert_htmlbox`), which is a few points taller than the *glyph-only*
extent `get_text("dict")` reports back on re-extraction -- CSS reserves
space above the first line's cap-height and below the last line's
descender beyond the actual glyphs. That box-height value fed
`prev_new_y1`, the anchor `place()`'s reflow chain uses to compute the
*next* paragraph's position -- so a later reformat-only pass's
re-extraction read the paragraph's inflated *rendered* gap back as "the
original gap," preserved it, and then added a fresh instance of the same
box-vs-glyph excess on top. Confirmed both mechanisms independently:
removing the manual `+2` alone (audit's own repro: simple separated
paragraphs) already fixed idempotency in that specific case, but a more
complex fixture (the golden fixture's own footnote/body mix) kept drifting
~0.1-0.15pt/pass with the `+2` removed but the box-height-anchoring bug
still present -- confirming the box-height mismatch as the deeper root
cause, with the manual pad as an *additional*, smaller contributor.

**Fix, two parts:** (1) dropped the manual `+2` entirely -- `scale_low=0`
on the real `insert_htmlbox` call remains the safety net for genuine
rounding disagreement (see Finding 11). (2) `measure_height` now returns
`(box_height, tight_height)`: `box_height` (the CSS line-box figure) still
sizes the actual insertion rect, so nothing clips; `tight_height` (the real
glyph-bbox extent, computed by reading the scratch page's own rendered
lines back) is what `prev_new_y1` is now anchored to, matching exactly what
a future re-extraction will see. `fit_placements_to_page`'s rescale loop
keeps using `box_height` for its own rect sizing -- a page that's been
rescaled once should fit at scale 1.0 on the next pass and not need
rescaling again, so this narrower edge case wasn't chased further here.

**Verified:** a 6-paragraph fixture (separate `<p>` blocks, real 14pt
gaps) run through 5 successive `--reformat-only` passes: body block height
`301.957` on every single pass, not just "within 1pt" but bit-for-bit
identical. Confirmed the fix is load-bearing by reverting the
`tight_height` anchoring alone (keeping `box_height` for the chain, the
pre-fix behavior) -- the idempotency test fails immediately. Added as a
permanent case in `tests/test_layout.py`, per the audit's own framing of
this as "the single highest-value test to add."

### 4. P2 — `_REFUSAL_RE` false-positived on ordinary German prose — Fixed + verified

`_REFUSAL_RE` matched any output starting with "please"/"sorry"/"I
can(not)" -- exactly how "Bitte beachten Sie..." ("Please note..."), "Es
tut mir leid, sagte er..." ("Sorry, he said...") and "Ich kann diese These
nicht teilen..." ("I cannot share this thesis...") legitimately translate.
Consequence: a spurious warning on good output, and at any non-zero
`--temp`, a wasted full model call on the retry path.

**Fix:** `looks_like_refusal()` now requires the opener to be followed,
within an 80-character prefix, by task-shaped vocabulary a genuine
translation-refusal would use ("provide", "translat*", "text", "German").
An initial attempt also gated on the *source* not itself opening with a
German polite trigger (the audit's own suggested fix) -- rejected after
testing, because it incorrectly suppressed the genuine-refusal case where
the source itself happens to be phrased as a meta-request ("Bitte geben
Sie den deutschen Text an." -> "Please provide the German text."): the
task-vocabulary check alone already discriminates every case correctly
without that extra, over-broad guard.

**Verified** against all four of the audit's own cases: the three
false-positive prose examples now correctly return `None`; the genuine
refusal example is still correctly flagged, including specifically the
variant where the source also opens with "Bitte" (`tests/test_units.py`).

### 5. P2 — bold is silently dropped and rendered as italic instead — Documented, not implemented

`span_is_italic()` checks `flags & 2`; PyMuPDF's bold bit is `flags & 16`
and nothing checks it, so a bold heading or emphasis run renders as plain
text. Doubly lost: `text_to_html` also normalizes the model's own
`**bold**` output down to a single `*` (i.e. into *italic*), so even
correctly-emitted bold comes out wrong.

**Decision:** documented rather than implemented, per the audit's own
offered alternative ("the cheaper honest option is to document it").
Proper support means reusing the asterisk-marking mechanism for a second,
nested delimiter (`**bold**` vs. `*italic*`, plus a `***both***` case),
a bold (and probably bold-italic) member in `FONT_CANDIDATES` and
`font_setup()`'s CSS, and re-deriving `join_paragraph_lines`' and
`ASTERISK_RUN_RE`'s hyphen/italic-boundary logic for two independently
nestable delimiters -- meaningfully larger and riskier than this round's
other fixes. Added to README.md's "Known limitations": bold is not carried
over, and the model's own bold output renders as italic.

### 6. P2 — `para["font"]` was dead data implying font fidelity that doesn't exist — Fixed (deleted) + verified

Assigned and stored, never read anywhere else. Every paragraph renders in
whichever serif pair `font_setup()` resolves regardless of the source's
actual typeface -- a defensible choice for this project's target document
class, but the stored-and-never-read field read as though the source
typeface were honored when it isn't.

**Fix:** deleted the field (the honest cheap option the audit offered).
Documented the single-typeface assumption in both
`split_page_into_paragraphs`'s docstring and README.md's "Known
limitations."

**Verified:** full test suite still passes with the field removed --
confirms nothing was actually reading it, as the original `grep` claimed.

### 7. P2 — `bad_translation_reason`'s length-ratio checks had no minimum-length floor — Fixed + verified

The echo check was guarded by `len(t) > 40`; the `<0.35x`/`>3.0x` ratio
checks were not, so a short paragraph where German's compound-word
shortening happens to swing the ratio close to the threshold (`"Abkuerzu
ngsverzeichnis"` -> `"Abbreviations"` is 0.59x; margin thinner than it
looks) risked a false "probable truncation" on exactly the paragraphs
(headings) where that's most confusing.

**Fix:** applied the same `len(t) > 40` floor to both ratio checks.

**Verified:** all four of the audit's own compound-word examples
(`Inhaltsverzeichnis`->`Contents`, `Geschwindigkeitsbegrenzung`->`Local
speed limit`, etc.) now correctly return `None`.

### 8. P2 — CI didn't run `tests/test_layout.py` — Fixed + verified

`.github/workflows/test.yml` ran `test_golden.py` and `test_units.py`
only -- `test_layout.py`, covering Round 4's overflow/pinning/collision
logic and reformat-only end to end, was silently never run in CI. Folded
all three into one step (each run explicitly, with exit codes combined at
the end) rather than three separate `- run:` steps, so a failure in one
file doesn't hide whether the others also failed -- GitHub Actions' default
`bash -e` would otherwise abort the whole step at the first non-zero exit.

**Verified:** ran the combined shell logic locally with all three files
passing (all exit 0) and confirmed the job would report a single failure
correctly when a file's exit code is non-zero (tested by temporarily
breaking one).

### 9. P2 — `--temp`/`--seed` were undocumented — Fixed

Added to the "Standalone usage" section of README.md, with the same
determinism rationale already in `translate()`'s own docstring.

### 10. P2 — the watcher's retry marker could be stranded permanently — Fixed + verified

`RETRY_MARKER` was cleared in exactly one place (`_main()`'s happy-path
`else` branch); if `_main()` raised anywhere after the marker was written
(`commit()`/`save_state()`/`log_result()`/`cleanup_stale_part_files()` are
all outside a `try`), or the detached retry child simply never ran (sleep,
reboot, kill), the marker stranded forever -- `schedule_retry()` returns
early whenever it exists, with nothing to ever clear it again, silently
defeating Round 2 leftover #4 for every future slow copy.

**Fix:** the marker already stores its own write time; `schedule_retry()`
now reads it back and treats a marker older than `STALE_RETRY_MARKER_AGE`
(3x `RETRY_DELAY`) as stranded rather than live, scheduling a fresh retry
instead of returning early. An unreadable marker is also treated as stale.
Did not add the audit's secondary suggestion (also clear the marker in a
`finally` in `main()`) -- on reflection that would undermine the dedup
guarantee this mechanism exists for: an unconditional clear on every exit
would let a crash-then-immediately-re-triggered run spawn a *second*,
redundant retry child before the first one even fires, which the
age-based expiry alone doesn't risk.

**Verified:** a fresh marker correctly deduplicates (no subprocess
spawned); a marker manually backdated past `STALE_RETRY_MARKER_AGE`
correctly schedules a fresh retry.

### 11. P2 — `fit_and_insert` discarded the one signal that would catch a measure/render mismatch — Fixed + verified

`insert_htmlbox`'s return, `(spare_height, scale)`, was discarded; with
`scale_low=0`, MuPDF is free to shrink text arbitrarily to make it fit
rather than report a failure, so any real disagreement between
`measure_height`'s scratch-page prediction and the actual render (a
leading/font-fallback/cache-key discrepancy) would show up only as a
visually subtle shrunken paragraph -- invisible to every warning path
Rounds 1-5 built.

**Fix:** `fit_and_insert` now takes an optional `report` and warns when
`scale < 0.999`, threaded in from `process_pdf`'s own `report()`. Same
argument Round 4 Finding 1 made for `report()` itself: turns a class of
future layout regressions grep-able instead of silent.

**Verified:** a deliberately undersized rect correctly reports `"paragraph
rendered at 0.42x -- measure_height disagreed with the actual render"`; a
normal, correctly-sized render reports nothing.

### 12. P2 — the truncation heuristic compared characters against tokens — Fixed + verified

`len(out) > max_tokens * 2` compares a **character** count against a
**token** budget; at ~4 chars/token, `* 2` fires around 50% of budget, not
"most of it" as the comment claimed.

**Fix:** counts the actual output tokens via `tokenizer.encode(out)` and
compares against `max_tokens * TRUNCATION_BUDGET_FRAC` (0.9) directly,
removing the character-based estimate entirely for the normal case; falls
back to a named `CHARS_PER_TOKEN` (4) estimate only if `encode()` itself
raises.

**Verified**, after initially being marked "by inspection" only:
extracted the whole truncation-detection block out of `translate()` (which
imports `mlx_lm` for real and can't be exercised without an actual model)
into a standalone `check_truncation(tokenizer, german_text, out,
max_tokens, report)`, callable with a stub tokenizer whose `encode()`
approximates real BPE behavior (~1 token/word, plus one per punctuation
mark, not `CHARS_PER_TOKEN`'s own ratio, so the test doesn't just
reconstruct the same estimate it's meant to replace). Four cases in
`tests/test_units.py`: a real near-budget unfinished output is flagged; the
same output isn't when `max_tokens` leaves headroom; an output that itself
ends in punctuation isn't flagged even near budget; and no check fires at
all when the source didn't end in punctuation either.

### 13. P2 — no packaging; tests are hand-rolled scripts — Fixed (both cheap wins)

Added a minimal `pyproject.toml` declaring `requires-python = ">=3.10"`
(the actual floor: `scripts/auto_translate_pdf.py`'s `str | None`
annotation needs 3.10 without the `from __future__ import annotations`
guard it happens to have; nothing previously declared a supported version
at all) and the split base/mlx dependencies as `[project.dependencies]`/
`[project.optional-dependencies]`.

For pytest-collectibility: all three test files ran their checks as
unguarded module-level code with a trailing bare `sys.exit()` -- collecting
them via `pytest tests/` would have raised `SystemExit` during import and
failed collection entirely, before this fix. Wrapped each file's final
print-and-exit block in `if __name__ == "__main__":` and added one
synthetic `test_*` function per file (`assert not failures`, or `assert
run()` for `test_golden.py`, which already had a `run()` wrapper) so
`pytest tests/` can collect and run them without changing any of the
existing checks' logic, output, or standalone `python tests/test_x.py`
behavior. Not installed as a project dependency (not currently used
anywhere), so untested against a real pytest run -- verified instead by
confirming all three files now import cleanly with zero side effects
(no `SystemExit`, no checks silently skipped) and that direct execution
(`python tests/test_x.py`) still produces identical output and exit codes.

### 14. P2 — rotated pages had no fixture — Fixed + verified

`AUDIT_FIXES.md` (Round 4 Finding 17) recorded `/Rotate 90` as spot-checked
by hand and "an accident of PyMuPDF's coordinate handling, not something
the code reasons about" -- with findings 2 and 3 above both changing how
rects are computed, "works by accident, verified by hand, untested" was
exactly the setup to pin down first.

**Fix:** added a `/Rotate 90` fixture to `tests/test_layout.py`,
confirming why it works: `get_text("dict")`'s span bboxes and `page.rect`
are *both* already in the rotated/display coordinate space, not the
underlying mediabox, so extraction, redaction, and `insert_htmlbox` all
agree without the pipeline needing to reason about the rotation transform
at all.

**Verified:** a rotated fixture round-trips through `process_pdf` with
`/Rotate 90` preserved in the output and translated content intact.

---

All 14 of Round 5's findings are addressed: 13 fixed and verified (12 was
initially marked "by inspection only" for lack of a real tokenizer here,
then later given a proper extraction + stub-tokenizer test -- see its
updated entry above), and 1 documented rather than implemented (5, bold
support -- explicitly offered as a legitimate alternative to a
meaningfully larger, riskier change; **still open as of this writing**,
tracked in "What's still open" below). Finding 6 (dead `font` field) was
fixed by deletion rather than by implementing font fidelity, also per the
audit's own offered alternative.

---

## Layout support — columns, images, tables (LAYOUT_SUPPORT.md)

Implements the region layer proposed in `LAYOUT_SUPPORT.md`: a new
`layout.py` module splits each page into ordered flow `regions` (columns,
a full-width heading band), `obstacles` (images/vector figures text must
never be drawn over or redacted through), and `tables` (translated cell by
cell). `translate_pdf.py`'s paragraph-splitting, reflow, and redaction were
refactored to operate per-region instead of per-page; a single-column,
figure-free, table-free page still produces exactly one region, so nothing
changes for that case. See README.md's new "Layout support" section for
the user-facing description and `CLAUDE.md` for the architecture notes
aimed at future changes to this code.

Phases 0-3 and 5 of the plan were implemented (test corpus + `debug_layout.py`
overlay renderer, `layout.py` itself, the region-aware refactor, retiring
the old skip-or-`--force` multi-column path, and table translation). Phase
6 (cross-column paragraph continuation) was not attempted — the plan itself
flags it as "the one genuinely hard problem... it will be imperfect and it
is the only part of this plan that can make output worse than the current
behaviour," to ship behind a flag defaulted off only once trusted; nothing
here needed it, and building it without dedicated verification would be
exactly the kind of half-tested layout code this whole feature exists to
avoid shipping.

Two real bugs surfaced during the refactor, neither anticipated by the
plan's own code (which predates this repo's Round 4/5 fixes -- leading,
box/tight-height split -- so its snippets had to be adapted, not copied):

1. **A text region's `rect` from `layout.analyze_page()` is a *tight*
   bounding box around existing content, not the space available to grow
   into.** Passed straight through as the fit limit, any paragraph growth
   at all (even a sub-point rounding difference between reformat passes)
   read as "region overflow," forcing a rescale a single-column page never
   needed before this refactor and breaking Round 5 Finding 3's
   reformat-only idempotency fix again. Fixed by deriving a separate
   `fit_rect`/`growth_ceiling` in `process_pdf` -- the next region's top,
   or the page's bottom margin if nothing is in the way -- rather than
   using the analyzed region rect directly for fitting purposes.
2. **`page.apply_redactions(fill=(1,1,1))` bakes a solid white rectangle
   into the page as real vector content.** On any later pass over this
   pipeline's own output (a second `--reformat-only` run, or the watcher
   re-triggering), `find_obstacles()` picked up that white-out box as a
   "figure" the same size and position as the very paragraph it covers,
   and `apply_text_redactions` then skipped redacting that area to avoid
   "redacting through a figure" -- leaving the old text underneath
   un-erased while new text was drawn on top of it, compounding every
   pass (18 lines -> 36 -> 46 -> 78 -> 140... on one test fixture, purely
   from re-detecting its own prior output as an obstacle). Fixed by
   excluding solid-white, borderless-or-white-stroked drawings from
   `find_obstacles()` -- a real figure is essentially never that.

**Verified:** all four pre-existing test files pass; two new ones added
(`tests/test_regions.py` for `layout.py` directly against a generated
corpus, extensions to `tests/test_layout.py` for table translation,
figure/obstacle survival across repeated reformat-only passes, and real
multi-column translation without row-by-row zipping). Both bugs above were
caught by running `--reformat-only` for 7+ successive passes on a
synthetic fixture and confirming the output geometry/line-count stayed
exactly stable, not just "close enough" -- the same idempotency-testing
approach Round 5 Finding 3 established.

---

## What's still open

- **Bold support (Round 5 Finding 5).** Bold text still renders as italic
  or plain, and the model's own `**bold**` output still collapses to
  *italic*. Documented in README's "Known limitations" as a deliberate
  cheaper alternative to implementing it, per the audit's own offer -- not
  attempted yet, tracked here in case that decision is revisited.
- **Cross-column paragraph continuation (`LAYOUT_SUPPORT.md` Phase 6).** A
  paragraph that starts at the bottom of one column and continues at the
  top of the next is still translated as two disconnected fragments, each
  without the other's context. The plan itself calls this "the one
  genuinely hard problem" and the only part of the whole design that could
  make output *worse* than today without dedicated verification -- meant
  to ship behind a flag (`--join-columns`) defaulted off, only once
  trusted. Not attempted yet.
