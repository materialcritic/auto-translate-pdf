#!/usr/bin/env python3
"""
Layout-preserving German -> English PDF translator.

Usage:
    venv/bin/python translate_pdf.py input.pdf output.pdf [--pages 1-5] [--model mlx-community/translategemma-4b-it-4bit]

Approach (see README.md for the full writeup):
  1. Extract text blocks per page with PyMuPDF, keeping bbox + font info.
  2. Split the page's lines into paragraphs, flattened across blocks, using
     indentation/size/gap heuristics (see split_page_into_paragraphs).
  3. Translate each paragraph with TranslateGemma (run locally via mlx-lm).
     Its chat template takes only the text itself -- no free-form context.
  4. Redact the page's whole text footprint and reflow all paragraphs back
     in page-wide, preserving each one's original gap to the next exactly.
"""

import argparse
import functools
import html
import os
import re
import sys
from pathlib import Path

# Speeds up the ~2GB one-time model download. HF_HUB_ENABLE_HF_TRANSFER
# (what an older huggingface_hub wanted) is deprecated as of the pinned
# 1.28.0 -- it now warns and does nothing, having moved to Xet transfer,
# enabled with this var instead. Must be set before huggingface_hub is
# imported, which happens lazily inside load_model's `from mlx_lm import
# load` -- hence setting it here, at module load.
os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")

import pymupdf as fitz  # noqa: E402 -- `import fitz` is deprecated as of PyMuPDF
# 1.24+; this import is deliberately placed after the HF_XET_HIGH_PERFORMANCE
# os.environ.setdefault() above (it must run before huggingface_hub is
# imported), not left here by oversight -- hence the noqa rather than
# reordering to satisfy the linter.

import layout  # noqa: E402 -- page-structure detection (columns/obstacles/tables)

DEFAULT_MODEL = "mlx-community/translategemma-4b-it-4bit"

# Fallback values used only when a paragraph has no spans to read a real
# size/font from at all (an edge case, not the common path) -- named here
# instead of repeating the same two magic literals at each call site.
FALLBACK_FONT_SIZE = 10.0
FALLBACK_FONT_NAME = "Times-Roman"


DEFAULT_TEMP = 0.0  # deterministic by default -- see translate()'s docstring
DEFAULT_SEED = 0


def load_model(model_name, seed=DEFAULT_SEED):
    from mlx_lm import load
    import mlx.core as mx
    # Seeded explicitly so a run is reproducible even at a non-zero --temp
    # (translation is a one-right-answer task; DEFAULT_TEMP=0.0 makes this
    # a no-op in the default case, but --temp above 0 would otherwise still
    # be unrepeatable run to run).
    mx.random.seed(seed)
    print(f"Loading {model_name} ...", file=sys.stderr)
    model, tokenizer = load(model_name)
    # The tokenizer's default eos_token_id doesn't include <end_of_turn>,
    # which is what this chat template actually emits to end a response --
    # without this, generation runs to max_tokens on every paragraph.
    #
    # mlx-lm's own add_eos_token() looks like a loud guard against this (it
    # raises if convert_tokens_to_ids returns None) but the guard is
    # fictional: HF's convert_tokens_to_ids returns unk_token_id, not None,
    # for a token that isn't in the vocab, so that check never fires and an
    # unrelated unk id gets silently registered as an EOS token instead --
    # strictly worse than the failure it was meant to catch. Verify the id
    # explicitly before handing it to add_eos_token.
    try:
        eot = tokenizer.convert_tokens_to_ids("<end_of_turn>")
    except Exception as e:
        raise RuntimeError(f"Could not resolve <end_of_turn> for {model_name}: {e}") from e
    unk = getattr(tokenizer, "unk_token_id", None)
    if eot is None or (unk is not None and eot == unk):
        raise RuntimeError(
            f"<end_of_turn> is not a real token for {model_name}. Without it "
            "every generation runs to max_tokens."
        )
    tokenizer.add_eos_token("<end_of_turn>")
    return model, tokenizer


_SENTENCE_END_RE = re.compile(r'[.!?"\')\]]\s*$')
CHARS_PER_TOKEN = 4          # rough English average; only used if tokenizer.encode(out) fails
TRUNCATION_BUDGET_FRAC = 0.9  # only suspect truncation once near the token cap, not merely over half of it


def translate(model, tokenizer, german_text, temp=DEFAULT_TEMP, report=None):
    """temp=0.0 (the default) is deterministic: this is a one-right-answer
    task, not a creative one, so sampling at temp=0.3 (the old default)
    bought nothing and cost reproducibility, run-to-run comparability, and
    the ability to bisect a rendering bug against a fixed translation --
    exactly the property reformat-only mode's own rationale relies on
    ("re-translating would introduce fresh non-determinism into text
    that's already correct"). Pass a non-zero temp explicitly (--temp on
    the CLI) if sampling is wanted; load_model() seeds the RNG either way
    so even a non-zero temp stays repeatable run to run."""
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler

    # The private-use sentinel that stands in for a literal "*" (see
    # ASTERISK_SENTINEL) is an internal extraction<->render carrier, not
    # something that should travel through the LLM at all -- nothing
    # guarantees a model tokenizes, preserves, or avoids duplicating an
    # obscure Unicode private-use codepoint faithfully. Swap it for a
    # plainer placeholder just for this round trip and restore it
    # immediately on return.
    german_text = strip_sentinel_for_model(german_text)

    # TranslateGemma's chat template requires this exact structured content
    # (source_lang_code/target_lang_code/text) -- it does not accept a
    # system prompt or free-form instructions.
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "source_lang_code": "de",
                    "target_lang_code": "en",
                    "text": german_text,
                    "image": None,
                }
            ],
        }
    ]
    prompt = tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, tokenize=False
    )

    # Size the budget off the source rather than a fixed constant. A dense
    # German academic paragraph (2500-3500 chars is entirely normal for a
    # footnote block or a long body paragraph) can exceed a fixed 1024-token
    # cap; nothing else in the pipeline notices, since the only existing
    # near-empty guard checks for too LITTLE content, never too much -- a
    # paragraph cut off mid-sentence gets inserted into the PDF looking
    # complete. 2.5x the source token count plus a fixed pad covers English's
    # typical expansion over German (usually mild, occasionally larger for
    # short idiomatic phrases) without an unbounded ceiling.
    try:
        n_src = len(tokenizer.encode(german_text))
    except Exception:
        n_src = max(1, len(german_text) // 3)  # rough fallback, ~3 chars/token
    max_tokens = max(256, min(4096, int(n_src * 2.5) + 64))

    sampler = make_sampler(temp=temp)
    out = generate(
        model, tokenizer, prompt=prompt, max_tokens=max_tokens, sampler=sampler, verbose=False
    )
    out = restore_sentinel_from_model(out.strip())
    check_truncation(tokenizer, german_text, out, max_tokens, report)
    return out


def check_truncation(tokenizer, german_text, out, max_tokens, report):
    """report()s a warning if `out` looks like it was cut off at
    `max_tokens` rather than actually finished. Split out from translate()
    itself specifically so this logic is unit-testable with a stub
    tokenizer/report list (translate() imports mlx_lm for real and can't
    be exercised without an actual model) -- see tests/test_units.py.

    mlx_lm.generate's plain-string return doesn't expose a finish reason,
    so this approximates "did this hit the token budget" by checking
    whether the output ends in sentence-final punctuation when the source
    did, and is already using most of max_tokens (a short, legitimately
    unpunctuated fragment shouldn't false-alarm). Counts the output's own
    *tokens* directly via the same tokenizer rather than estimating from
    character count against a token budget -- comparing len(out)
    (characters) against max_tokens (tokens) via a fixed multiplier either
    over- or under-fires depending how well that multiplier matches the
    real chars-per-token ratio for whatever text actually came back;
    encoding the real output removes the estimate entirely."""
    if not (report and _SENTENCE_END_RE.search(german_text.strip())
            and not _SENTENCE_END_RE.search(out)):
        return
    try:
        n_out = len(tokenizer.encode(out))
    except Exception:
        n_out = None
    near_budget = (n_out is not None and n_out > max_tokens * TRUNCATION_BUDGET_FRAC)
    if n_out is None:
        # encode() unavailable for some reason -- fall back to the
        # character-count estimate rather than skipping the check
        # entirely, at CHARS_PER_TOKEN chars/token (a rough English
        # average).
        near_budget = len(out) > max_tokens * CHARS_PER_TOKEN * TRUNCATION_BUDGET_FRAC
    if near_budget:
        report(f"  WARNING: translation may be truncated at max_tokens="
               f"{max_tokens} (source ended in punctuation, output did not): "
               f"...{out[-60:]!r}")


def line_text(line):
    return "".join(s["text"] for s in line["spans"])


def dominant_size(lines):
    """Font size covering the most characters across these lines -- i.e.
    the body text size, as opposed to footnote-marker superscripts etc."""
    totals = {}
    for l in lines:
        for s in l["spans"]:
            totals[s["size"]] = totals.get(s["size"], 0) + len(s["text"])
    if not totals:
        return FALLBACK_FONT_SIZE
    return max(totals, key=totals.get)


def span_is_italic(span):
    font = span.get("font", "")
    return bool(span["flags"] & 2) or "italic" in font.lower() or "oblique" in font.lower()


SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹",
                                    "0123456789")


def as_marker_digits(text):
    r"""The digits of `text` if it is nothing but digits -- ASCII ("12") or
    Unicode superscripts ("¹²") -- else None.

    str.isdigit() is True for "¹" (superscript 1), so a superscript
    marker would pass a naive digit test but get emitted verbatim as
    "[¹]" -- which FOOTNOTE_MARKER_RE (\d, i.e. Unicode category Nd
    only) can never match again: the marker could not be counted, restored
    if translation dropped it, or stripped by the near-empty-paragraph
    check. Normalizing to ASCII first keeps every downstream [N] consumer
    working."""
    stripped = text.strip()
    if not stripped:
        return None
    ascii_digits = stripped.translate(SUPERSCRIPT_DIGITS)
    if ascii_digits.isdecimal() and len(ascii_digits) <= 3:
        return ascii_digits
    return None


def span_is_footnote_marker(span, dominant_size):
    """A footnote reference number: digits-only, and either set smaller than
    the body text, written with Unicode superscript glyphs (which carry the
    superscripting in the characters themselves, so they're often set at
    full body size and would fail a size-only test), or flagged as a real
    PDF superscript by the typesetter -- which commonly lands at ~0.83x
    body size, in the dead zone between "small enough to catch on size
    alone" and "full body size" that the first two checks leave uncovered.
    PyMuPDF sets bit 0 of a span's `flags` for superscript text; nothing
    used to ask it. Returns the ASCII digit string, or None if this span
    isn't a marker."""
    digits = as_marker_digits(span["text"])
    if digits is None:
        return None
    is_superscript = bool(span.get("flags", 0) & 1)  # PyMuPDF: bit 0 = superscript
    if span["size"] < dominant_size * 0.8 or span["text"].strip() != digits or is_superscript:
        # NOTE: this now also matches a genuine mathematical superscript
        # exponent ("x<sup>2</sup>", "10<sup>6</sup>") -- also digits-only,
        # also flagged superscript. Left permissive rather than requiring
        # "glued to the end of a word/number" (no preceding space) because
        # the near-empty guard and the "[N]" round trip make the
        # consequence cosmetic (a wrongly-marked exponent still survives
        # translation, just wrapped as "[2]" instead of rendered as a
        # literal superscript "2") rather than corrupting -- unlike a real
        # marker going undetected, which silently fuses into the adjacent
        # digits (see this function's docstring / Round 5 Finding 1).
        return digits
    return None


ASTERISK_SENTINEL = ""  # private use area; internal extraction->render carrier only
# Sent through the LLM in ASTERISK_SENTINEL's place (see
# strip_sentinel_for_model/restore_sentinel_from_model below). An LLM has no
# particular reason to tokenize, preserve, or avoid duplicating an obscure
# Unicode private-use codepoint faithfully; this plain, distinctive,
# unlikely-to-occur-naturally ASCII token is a smaller bet on the model's
# behavior, and it's swapped back to the sentinel immediately on return, so
# it never reaches text_to_html or anything else that reasons about "*".
ASTERISK_MODEL_PLACEHOLDER = "XASTERISKX"


def strip_sentinel_for_model(text):
    """Swap the internal sentinel for ASTERISK_MODEL_PLACEHOLDER right
    before handing `text` to translate() -- see ASTERISK_MODEL_PLACEHOLDER."""
    return text.replace(ASTERISK_SENTINEL, ASTERISK_MODEL_PLACEHOLDER)


def restore_sentinel_from_model(text):
    """Inverse of strip_sentinel_for_model, applied immediately to
    translate()'s return value -- the sentinel never actually leaves this
    process except for that one round trip."""
    return text.replace(ASTERISK_MODEL_PLACEHOLDER, ASTERISK_SENTINEL)


def line_text_marking(line, dominant_size):
    """Like line_text, but with two kinds of markup injected as plain-text
    markers so they survive the trip through the translation model:

    - A footnote-marker span (see span_is_footnote_marker) is rewritten as
      an explicit " [N] " token. Left as raw text, it would just get
      silently absorbed into the adjacent number/word (e.g. "1938" + "1" ->
      "19381") when spans are naively concatenated.
    - An italic span is wrapped in "*...*". TranslateGemma reliably passes
      this markdown-style emphasis through translation intact (verified
      empirically -- it already produces this style on its own for things
      like book titles), which plain italic formatting has no way to
      survive since the model only sees a text string.

    A literal "*" already present in the source (German uses "* 1903" for a
    birth date, or plain arithmetic like "2 * 4") is indistinguishable from
    an italic delimiter once spans are flattened to a string, and would get
    paired with the next one, italicizing everything in between. It's
    parked on a private-use sentinel that no real text contains and that
    survives translation as an opaque character, then restored at render
    time (see text_to_html).
    """
    parts = []
    for s in line["spans"]:
        t = s["text"]
        stripped = t.strip()
        marker = span_is_footnote_marker(s, dominant_size) if stripped else None
        if marker is not None:
            parts.append(f" [{marker}] ")
        elif stripped and span_is_italic(s):
            lead = t[: len(t) - len(t.lstrip())]
            trail = t[len(t.rstrip()):]
            # A literal "*" inside the span would close this italic run early
            parts.append(f"{lead}*{stripped.replace('*', ASTERISK_SENTINEL)}*{trail}")
        else:
            parts.append(t.replace("*", ASTERISK_SENTINEL))
    return "".join(parts)


# German suspended compounds keep their hyphen even when the break falls
# right after it: "Sozial-" + "und Wirtschaftsgeschichte".
_SUSPENSION_RE = re.compile(r"^\*?(und|oder|bzw\.?|sowie|wie|beziehungsweise)\b", re.I)


def join_paragraph_lines(pieces, dehyphenate=True):
    """Join a paragraph's extracted lines, re-fusing only words that the
    source PDF actually hyphenated at a line break.

    Done at the join, where line boundaries still exist, rather than with a
    global `re.sub(r"-\\s+", "")` over the already-joined string: that regex
    can't tell a line-break hyphen from any other hyphen followed by a
    space, so it corrupts suspended compounds ("Sozial- und" ->
    "Sozialund"), page ranges ("105-" + "109" -> "105109"), and hyphens used
    as dashes (deleted outright) -- all common in ordinary German prose, not
    just at paragraph-merge edge cases."""
    pieces = [p for p in pieces if p]
    if not pieces:
        return ""
    out = pieces[0]
    for nxt in pieces[1:]:
        stem = out.rstrip()
        tail = "*" if stem.endswith("*") else ""  # italic run closed at the break
        core = stem[:-1] if tail else stem
        opens_italic = nxt.startswith("*")
        nxt_core = nxt[1:] if opens_italic else nxt

        at_break_hyphen = (core.endswith("-") and len(core) >= 2
                           and not core[-2].isspace())
        if not at_break_hyphen:
            out = out + " " + nxt  # no line-break hyphen at all
        elif _SUSPENSION_RE.match(nxt_core):
            # Checked before the dehyphenate branch below: a suspended
            # compound keeps BOTH its hyphen and the following space in
            # either mode ("Sozial- und ...", never "Sozial-und ...").
            out = out + " " + nxt
        elif not dehyphenate:
            # Reformat-only mode: the hyphen is always a real character
            # (insert_htmlbox never hyphenates at a line break the way
            # German typesetting does, so it's a compound word, a name, or
            # a page range), so it's kept -- but the two halves are still
            # one word and must not be separated by a space ("Schulte-" +
            # "Sasse" is "Schulte-Sasse", never "Schulte- Sasse").
            if tail and opens_italic:  # "105-"+"109" across an italic boundary
                out = core + nxt_core  # merge the two runs; "**" would break parsing
            else:
                out = core + tail + nxt
        elif nxt_core[:1].islower():  # real word break -> fuse
            if tail and opens_italic:  # ...across an italic boundary
                out = core[:-1] + nxt_core
            else:
                out = core[:-1] + tail + nxt
        else:  # "105-"+"109", "Schulte-"+"Sasse"
            if tail and opens_italic:  # ...across an italic boundary
                # Same guard as the fuse branch above: "*Schulte-*" +
                # "*Sasse*" naively becomes "*Schulte-**Sasse*", which
                # text_to_html's "**"->"*" normalization then closes one
                # character early, leaving "Schulte-<i>Sasse</i>" -- the
                # first half silently loses its italics. Merging the two
                # runs into one keeps the whole fused word italic.
                out = core + nxt_core
            else:
                out = core + tail + nxt  # keep hyphen, drop the space
    return out


def detect_columns(lines, page_width, min_frac=0.25, min_gap_frac=0.15):
    """Deprecated. Kept only so `--check` diagnostics and any external
    caller written against the pre-layout.py pipeline keep working;
    layout.find_column_split is the real implementation now, and unlike
    this function it returns the column *rectangles*, which is what makes
    real multi-column reflow/translation possible in process_pdf, rather
    than this function's original job of deciding whether to skip or warn.

    Returns the number of detected column clusters (1 for ordinary
    single-column prose), derived from the lines' own combined bounding
    box exactly the way process_pdf's pre-region-layer code used to call
    this."""
    if not lines:
        return 1
    x0 = min(l["bbox"][0] for l in lines)
    x1 = max(l["bbox"][2] for l in lines)
    y0 = min(l["bbox"][1] for l in lines)
    y1 = max(l["bbox"][3] for l in lines)
    cols = layout.find_column_split(lines, fitz.Rect(x0, y0, x1, y1))
    return len(cols) if cols else 1


def modal_left_margin(lines):
    """The x0 shared by more lines than any other on the page -- i.e. the
    body-text left margin, used both to detect paragraph indentation here
    and as a `--check`-mode diagnostic (see check_pdf)."""
    x0s = [round(l["bbox"][0], 1) for l in lines]
    return max(set(x0s), key=x0s.count)


def split_lines_into_paragraphs(lines, dominant_size_override=None,
                                 left_margin_tolerance=4.0, gap_multiplier=1.7,
                                 dehyphenate=True):
    """
    Group lines into paragraphs using indentation as the primary signal: a
    line whose x0 is meaningfully greater than the *region's* modal left
    margin starts a new paragraph (standard academic-prose convention -- no
    blank line between paragraphs, just an indented first line).

    Takes lines directly rather than a page's raw blocks, so a caller can
    pass one region's lines instead of a whole page's (see layout.py) --
    this works on ALL of the given lines flattened together, not block by
    block, for the same reason either way: some PDF producers group a whole
    paragraph's lines into one PDF "block" (then per-block indentation
    detection would be enough), but others emit one block per line -- in
    that case a block never contains more than one line to compare margins
    against, so indentation can never be detected and every line would be
    mistaken for its own paragraph, stripping context from mid-sentence
    line breaks and producing fragments the translator has no hope of
    rendering coherently. Flattening first makes the two cases
    indistinguishable, which is what we want. `modal_left_margin` in
    particular must be computed per region, not page-wide: on a two-column
    page the page-wide modal margin is one column's flush margin, which
    makes every line in the *other* column look like a ~200pt indent and
    therefore its own paragraph.

    Two extra signals guard against merging things that just happen to
    share the body margin: a large jump in font size (heading dropped into
    the middle of body text) or an unusually large vertical gap (more than
    `gap_multiplier` line-heights) each also force a new paragraph.

    `dominant_size_override`, unlike the margin, stays page-wide when a
    caller has one to give (see split_page_into_paragraphs / layout-aware
    callers) -- it only feeds footnote-marker detection
    (span_is_footnote_marker), and a narrow column of footnotes would
    otherwise decide its own small type *is* the body size and stop
    recognizing its own markers.

    Deliberately doesn't record the source's typeface: every paragraph
    renders in whichever single serif roman+italic pair font_setup()
    resolves, regardless of the source document's actual font(s) -- a
    defensible choice for this project's target document class (academic
    prose is reliably one serif family throughout), but see "Known
    limitations" in README.md.
    """
    lines = [l for l in lines if l["spans"]]
    if not lines:
        return []
    # Sort by row, then left-to-right within the row. Several "line" dicts
    # can share a y (the wide-word-gap case `same_row` below handles), and a
    # y-only sort leaves those in extractor order, which is not guaranteed
    # to be reading order -- the fragments would then get concatenated
    # scrambled.
    lines.sort(key=lambda l: (round(l["bbox"][1], 1), l["bbox"][0]))

    x0s = [round(l["bbox"][0], 1) for l in lines]
    margin = modal_left_margin(lines)
    page_dominant_size = (
        dominant_size_override if dominant_size_override is not None
        else dominant_size(lines)
    )

    def body_size_of_line(line):
        """Like dominant_size([line]), but ignores footnote-marker-style
        spans (small + digit-only) -- a line consisting of *only* a
        trailing footnote marker (e.g. a lone superscript "1" that landed
        on its own line at a paragraph's tail) would otherwise report that
        marker's tiny font as the line's size, look like a size jump versus
        the preceding body text, and get sliced off into its own
        degenerate one-token "paragraph" that has nothing real to
        translate."""
        real_spans = [
            s for s in line["spans"]
            if span_is_footnote_marker(s, page_dominant_size) is None
        ]
        return dominant_size([{"spans": real_spans}]) if real_spans else None

    def starts_with_marker(line):
        """True if the line opens with a footnote reference number. Footnote
        and reference-list entries sit flush at the body margin, in the same
        size, one after another with normal leading -- none of the indent,
        size-jump or gap signals fire, so consecutive entries would
        otherwise be merged into a single paragraph and translated as one
        run-on blob. The leading marker is the only thing that
        distinguishes them."""
        for sp in line["spans"]:
            if not sp["text"].strip():
                continue
            return span_is_footnote_marker(sp, page_dominant_size) is not None
        return False

    paragraphs = []
    current_lines = []
    prev_size = None
    prev_y1 = None
    prev_y0 = None
    for line, x0 in zip(lines, x0s):
        size = body_size_of_line(line)
        y0 = line["bbox"][1]
        # Some PDFs (justified text with unusually wide word-cluster gaps)
        # get split by PyMuPDF into several "line" dict entries that all
        # sit at the same y0 -- they're really one physical line, e.g.
        # "Marcuse" / "verpflichteten" / "Deutungen," each as their own
        # entry despite reading as one continuous sentence. Treating each
        # as indentation (its x0 is way past the margin) would carve every
        # word cluster on that line into its own one-word "paragraph". Since
        # they're the same row, they always continue the current paragraph
        # regardless of what the indent/size/gap checks below would say.
        same_row = prev_y0 is not None and abs(y0 - prev_y0) < 2.0
        is_new_para = not same_row and (
            x0 > margin + left_margin_tolerance
            or starts_with_marker(line)
            or (prev_size is not None and size is not None and abs(size - prev_size) > 1.5)
            or (prev_y1 is not None and y0 - prev_y1 > gap_multiplier * (size or page_dominant_size))
        )
        if is_new_para and current_lines:
            paragraphs.append(current_lines)
            current_lines = []
        current_lines.append(line)
        if size is not None:
            prev_size = size
        prev_y1 = line["bbox"][3]
        prev_y0 = y0
    if current_lines:
        paragraphs.append(current_lines)

    result = []
    for para_lines in paragraphs:
        pieces = [
            line_text_marking(l, page_dominant_size).strip() for l in para_lines
        ]
        text = join_paragraph_lines(pieces, dehyphenate)
        # insert_htmlbox's own justification/hyphenation logic can rewrite a
        # real "-" inside a word into an invisible soft hyphen (U+00AD) even
        # when `hyphens: none` is set -- if that word is ever re-extracted
        # and re-rendered (e.g. reformat-only mode), the soft hyphen stays
        # invisible and the two halves read as one fused word ("Schulte-
        # Sasse" -> "SchulteSasse"). Always normalize back to a real hyphen.
        text = text.replace("\xad", "-")
        text = re.sub(r"\s+", " ", text).strip()
        # Tidies up a stray space before punctuation left by line-joining.
        # Assumes German/English punctuation conventions (no space before
        # ".,;:!?"); this would incorrectly strip a legitimate French-style
        # spaced punctuation mark (" ?", " !", " :") if a quoted French
        # passage appeared in the source. Low risk for this pipeline's
        # target documents (German academic prose), not a general-purpose
        # assumption.
        text = re.sub(r"\s+([.,;:!?])", r"\1", text)
        bbox = fitz.Rect(para_lines[0]["bbox"])
        for l in para_lines[1:]:
            bbox |= fitz.Rect(l["bbox"])
        # The size covering the most characters in the whole paragraph, not
        # the first span's. A footnote/reference entry starts with its
        # superscript marker, so the first span is the ~6.5pt marker while
        # the entry itself is ~9pt -- taking the first span rendered whole
        # footnote blocks at marker size, and also skewed page_body_size
        # (which drives the small-text/tight-run checks in process_pdf).
        size = dominant_size(para_lines) if para_lines[0]["spans"] else FALLBACK_FONT_SIZE
        # a paragraph that was a single line in the source is almost always
        # a heading/title/byline fragment, not real body prose -- justify
        # stretches those into ugly full-width letter-spacing if translation
        # makes them wrap, so they get left-aligned instead (see paragraph_html)
        single_line = len(para_lines) == 1
        # How far the paragraph's first line sits right of the paragraph's
        # own left edge (bbox.x0, the flush margin most of its lines sit
        # at) -- i.e. the source's first-line indent, which is the *only*
        # paragraph separator in the layout convention this splitter is
        # built around (no blank line between paragraphs). Recorded here so
        # it can be re-emitted at render time (see paragraph_html) instead
        # of being silently consumed as a detection signal and then thrown
        # away, which used to make every translated paragraph render flush
        # left -- visually one continuous block with no paragraph breaks at
        # all, even though the splitter had correctly found them.
        indent = round(max(0.0, para_lines[0]["bbox"][0] - bbox.x0), 1) if not single_line else 0.0
        # The source's own line spacing, as a multiple of its font size
        # (e.g. 1.36 for 15pt leading on 11pt type) rather than an absolute
        # point value -- paragraph_html emits this straight into CSS's
        # unitless `line-height`, which is itself already relative to
        # whatever font-size applies, so a paragraph that later gets
        # rescaled by fit_placements_to_page keeps the same *proportions*
        # for free with no extra math. Without this, every paragraph
        # rendered at MuPDF's user-agent default of 1.2x regardless of the
        # source's actual leading -- correct only by coincidence for a
        # document that happened to already be set at 1.2x, and off by as
        # much as 22% of a paragraph's height for one set looser (a bigger
        # miss than any of the inter-paragraph gap arithmetic Rounds 1-4
        # were built to get exactly right). The median of consecutive
        # line-start deltas (not the mean) so one anomalously large gap --
        # a widow line before a page break, an accidental double-line-break
        # in the source -- doesn't skew the whole paragraph's leading.
        leading_ratio = None
        if not single_line and size > 0:
            ys = [l["bbox"][1] for l in para_lines]
            deltas = sorted(b - a for a, b in zip(ys, ys[1:]) if b > a)
            if deltas:
                leading_ratio = round(deltas[len(deltas) // 2] / size, 3)
        result.append({
            "text": text, "bbox": bbox, "size": size,
            "single_line": single_line, "indent": indent, "leading": leading_ratio,
        })
    return result


def split_page_into_paragraphs(blocks, left_margin_tolerance=4.0, gap_multiplier=1.7,
                                dehyphenate=True):
    """Back-compat wrapper: the whole page (all its blocks' lines
    flattened) as one region. Kept for check_pdf and any external caller
    that predates the region-aware layout.analyze_page() path -- see
    split_lines_into_paragraphs for the real implementation."""
    lines = [l for b in blocks for l in b["lines"] if l["spans"]]
    return split_lines_into_paragraphs(
        lines, None, left_margin_tolerance, gap_multiplier, dehyphenate
    )


# A refusal opener alone ("please", "sorry", "I can('t)...") is not enough
# on its own: ordinary German academic prose translates into plenty of
# legitimate English sentences that start the same way -- an editorial note
# ("Bitte beachten Sie..." -> "Please note..."), quoted speech ("Es tut mir
# leid, sagte er..." -> "Sorry, he said..."), first-person argument ("Ich
# kann diese These nicht teilen..." -> "I cannot share this thesis...").
# What actually distinguishes a genuine refusal is that it *names the task*
# -- it talks about providing/translating a text, not about whatever the
# source's own sentence was about. Requiring the opener to be followed,
# within a short prefix, by that task-shaped vocabulary ("provide",
# "translate", "text", "German") cuts the false-positive rate on ordinary
# prose far more than gating on the source's own language would: a source
# that itself opens with "Bitte..." for an unrelated reason (the common
# case) still translates into an opener-only sentence with no task
# vocabulary and is correctly left unflagged, while a source that happens
# to itself be phrased as a meta request ("Bitte geben Sie den Text an.")
# is correctly still flagged, which gating on the source's own opening
# words would incorrectly suppress.
_REFUSAL_OPENER_RE = re.compile(r"^\s*(please|sorry|i\s+(?:can|am|cannot|can't))\b", re.I)
_REFUSAL_TASK_RE = re.compile(r"\b(provide|translat\w*|\btext\b|german)\b", re.I)
_REFUSAL_PREFIX_CHARS = 80  # how far into the output to look for task vocabulary


def looks_like_refusal(translated_text):
    """True if `translated_text` looks like a conversational refusal
    ("Please provide the German text you would like translated.") rather
    than an actual translation. See the module-level comment above for why
    an opener alone isn't sufficient."""
    if not _REFUSAL_OPENER_RE.match(translated_text):
        return False
    return bool(_REFUSAL_TASK_RE.search(translated_text[:_REFUSAL_PREFIX_CHARS]))


def bad_translation_reason(text, translated):
    """The near-empty guard prevents *sending* a degenerate request, but
    nothing used to inspect the *response* -- if TranslateGemma echoed the
    German unchanged, replied conversationally ("Please provide the German
    text you would like translated."), or produced a wildly truncated or
    runaway-repetitive output, it was inserted into the PDF verbatim as if
    it were a real translation. Returns a short reason string for report(),
    or None if the response looks plausible. Heuristic, not a guarantee --
    deliberately cheap checks meant to catch the obviously-wrong cases."""
    t, out = text.strip(), translated.strip()
    if len(t) > 40 and out == t:
        return "output is identical to the source (probable no-op/echo)"
    if len(t) > 40 and len(out) < 0.35 * len(t):
        return "output is much shorter than the source (probable truncation)"
    if len(t) > 40 and len(out) > 3.0 * len(t):
        return "output is much longer than the source (probable runaway repetition)"
    if looks_like_refusal(out):
        return "output looks like a conversational reply, not a translation"
    return None


FOOTNOTE_MARKER_RE = re.compile(r"\[(\d{1,3})\]")


def preserve_footnote_markers(source_text, translated_text):
    """If a [N] footnote marker present in the source didn't survive
    translation verbatim, append it at the end so the reference isn't
    silently lost (imperfect placement, but nothing is dropped)."""
    src = FOOTNOTE_MARKER_RE.findall(source_text)
    have = FOOTNOTE_MARKER_RE.findall(translated_text)
    missing = []
    for m in src:
        if m in have:
            have.remove(m)  # count-aware: a repeated [1] needs two survivors
        else:
            missing.append(m)
    if not missing:
        return translated_text
    return translated_text.rstrip() + " " + " ".join(f"[{m}]" for m in missing)

FONT_CANDIDATES = [
    ("/System/Library/Fonts/Supplemental", "Times New Roman.ttf", "Times New Roman Italic.ttf"),
    ("/Library/Fonts", "Times New Roman.ttf", "Times New Roman Italic.ttf"),
    ("/usr/share/fonts/truetype/msttcorefonts", "Times_New_Roman.ttf", "Times_New_Roman_Italic.ttf"),
    ("/usr/share/fonts/truetype/liberation", "LiberationSerif-Regular.ttf", "LiberationSerif-Italic.ttf"),
]

_FONT_STATE = None


def font_setup():
    """(archive, css) for the first available serif roman+italic pair.

    Built on first use, not at import: fitz.Archive raises on a missing
    directory, and at module scope that turns a fixable configuration
    problem into an unimportable module -- e.g. the watcher's `from
    translate_pdf import ...` would die before it could log anything."""
    global _FONT_STATE
    if _FONT_STATE is not None:
        return _FONT_STATE
    for d, roman, italic in FONT_CANDIDATES:
        if os.path.isfile(os.path.join(d, roman)) and os.path.isfile(os.path.join(d, italic)):
            css = f"""
                @font-face {{ font-family: body; src: url("fonts/{roman}"); }}
                @font-face {{ font-family: body; font-style: italic;
                             src: url("fonts/{italic}"); }}
                /* An element selector is required here: `*` has specificity
                   0 and loses to MuPDF's user-agent rule `p {{ margin: 1em 0 }}`,
                   which would otherwise silently add 2em of vertical margin
                   to every paragraph -- a constant per-paragraph gap that no
                   amount of gap arithmetic in place() can compensate for. */
                p, div, body {{ margin: 0; padding: 0; }}
                * {{ font-family: body; margin: 0; padding: 0; hyphens: none; }}
            """
            _FONT_STATE = (fitz.Archive(d, "fonts"), css)
            return _FONT_STATE
    raise RuntimeError(
        "No serif roman+italic font pair found. Tried:\n  "
        + "\n  ".join(d for d, _, _ in FONT_CANDIDATES)
        + "\nAdd your font directory to FONT_CANDIDATES in translate_pdf.py."
    )

# Markdown-style emphasis: the opening "*" must be followed by non-space and
# the closing "*" preceded by non-space, and neither may sit inside a word.
# A bare "*" used as a real character ("2 * 4", "* 1903") therefore never
# opens a run, unlike the old r"\*(.+?)\*", which paired any two asterisks
# positionally and would italicize everything between a literal one and the
# next.
ASTERISK_RUN_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])")


def text_to_html(text):
    """Turn a string containing '*italic*' markers into an HTML fragment
    with <i> tags, escaping everything else. Any asterisk that doesn't form
    a well-formed emphasis run (the model dropped one, or it was a literal
    asterisk to begin with) renders as a literal '*' rather than being
    deleted -- a real asterisk the model itself emits (a footnote star, a
    birth-date "* 1903", a markdown bullet) used to vanish silently, which
    is strictly worse than the purely cosmetic risk (an occasional stray
    '*') that deleting it was meant to avoid. This also used to be an
    all-or-nothing whole-string parity check, which meant one dropped
    asterisk anywhere killed italics for the entire paragraph."""
    # TranslateGemma sometimes emits markdown-style "**bold**"; normalize
    # runs of 2+ asterisks to one before parsing, or "**Dialektik**" becomes
    # "<i>*Dialektik</i>*" -- a stray literal "*" plus an italic run that
    # starts one character early.
    #
    # There used to be a second pass here collapsing "*<whitespace>*" to
    # drop "empty" runs left behind by the line above (e.g. "**  **" ->
    # "*  *"). But that pattern is indistinguishable from two genuinely
    # separate italic runs separated by an ordinary word gap -- "*Marx*
    # *Engels*" also contains a "*" + space + "*" at the boundary between
    # them, so the collapse silently merged the two into one run spanning
    # "Marx Engels". It's unnecessary anyway: ASTERISK_RUN_RE already
    # requires its opening "*" to be followed by a non-space character, so
    # a whitespace-only "run" like "*  *" never matches it in the first
    # place and both asterisks fall through to esc()'s unmatched-delimiter
    # drop below, with no empty <i> tag and no merging.
    text = re.sub(r"\*{2,}", "*", text)

    def esc(chunk):
        # Restore literal asterisks parked on the sentinel by
        # line_text_marking. Any OTHER leftover asterisk here is, by
        # definition, one ASTERISK_RUN_RE didn't pair up -- keep it as a
        # literal character (html.escape doesn't treat '*' as special)
        # instead of deleting it.
        return html.escape(chunk.replace(ASTERISK_SENTINEL, "*"))

    out = []
    pos = 0
    for m in ASTERISK_RUN_RE.finditer(text):
        out.append(esc(text[pos:m.start()]))
        out.append(f"<i>{esc(m.group(1))}</i>")
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


def paragraph_html(text, fontsize, single_line=False, indent=0.0, leading=None):
    body = text_to_html(text)
    align = "left" if single_line else "justify"
    # Re-emits the source's first-line indent (see split_page_into_
    # paragraphs) -- without it, every translated paragraph renders flush
    # left and the layout convention this whole splitter is built around
    # (no blank line between paragraphs, indent only) loses its only visual
    # separator: two indent-separated source paragraphs read as one
    # continuous block in the output. Single-line paragraphs are headings/
    # bylines, not indented body prose, so indent is always 0 for those.
    indent_style = f" text-indent:{indent}pt;" if indent else ""
    # Re-emits the source's own line spacing as a multiple of font size
    # (see split_page_into_paragraphs) -- CSS's unitless line-height is
    # itself relative to whatever font-size applies, so this stays correct
    # for free through fit_placements_to_page's rescale with no extra math
    # on this end. Without it, MuPDF's user-agent default of 1.2x applied
    # regardless of the source's actual leading.
    leading_style = f" line-height:{leading};" if leading else ""
    return (f'<p style="font-size:{fontsize}pt; text-align:{align};'
            f'{indent_style}{leading_style}">{body}</p>')


_SCRATCH_DOC = None


def _scratch_page(width, height=3000):
    """A page on a lazily-created, reused-for-the-process-lifetime scratch
    document. Opening a whole new fitz.Document per measurement (the old
    behavior) pays PDF-structure setup/teardown on every single call;
    reusing one document and just adding/dropping a page is materially
    cheaper across the hundreds of measurements a single document's worth
    of paragraphs needs, and thousands for fit_placements_to_page's rescale
    loop (see _measure_height_uncached)."""
    global _SCRATCH_DOC
    if _SCRATCH_DOC is None:
        _SCRATCH_DOC = fitz.open()
    return _SCRATCH_DOC.new_page(width=width + 1, height=height)


def _measure_height_uncached(width, text, fontsize, single_line, indent, leading):
    archive, css = font_setup()
    page = _scratch_page(width)
    try:
        rect = fitz.Rect(0, 0, width, 3000)
        spare_height, _ = page.insert_htmlbox(
            rect, paragraph_html(text, fontsize, single_line, indent, leading),
            css=css, archive=archive,
        )
        box_height = 3000 if spare_height < 0 else 3000 - spare_height
        # CSS's line-box model reserves some space above the first line's
        # cap-height and below the last line's descender beyond the glyphs
        # themselves (bigger for a looser leading) -- box_height (what
        # insert_htmlbox actually used) is a few points taller than the
        # *glyph-tight* extent get_text("dict") reports back on
        # re-extraction. Round 5 Finding 3: using box_height to advance the
        # reflow chain's "where does the next paragraph start" anchor
        # planted that few-point gap where reformat-only mode's *next* pass
        # would read it back as part of the "original" inter-paragraph gap
        # and preserve it -- plus add its own fresh instance of the same
        # gap on top, forever. Measuring the actual rendered glyph bbox
        # here keeps the reflow chain anchored to the same tight geometry
        # a re-extraction will see, so nothing is left to compound.
        lines = [
            l for b in page.get_text("dict")["blocks"] if b["type"] == 0
            for l in b["lines"] if l["spans"]
        ]
        tight_height = (max(l["bbox"][3] for l in lines) - min(l["bbox"][1] for l in lines)
                        if lines else box_height)
        return box_height, tight_height
    finally:
        _SCRATCH_DOC.delete_page(page.number)


@functools.lru_cache(maxsize=4096)
def _measure_height_cached(width, text, fontsize, single_line, indent, leading):
    return _measure_height_uncached(width, text, fontsize, single_line, indent, leading)


def measure_height(width, text, fontsize, single_line=False, indent=0.0, leading=None):
    """(box_height, tight_height) that `text` needs at `fontsize` in a box
    of given width, measured on a throwaway scratch page.

    `box_height` is what the CSS line-box model actually occupies (safe to
    build the *insertion* rect from, so nothing clips); `tight_height` is
    the glyph-only extent get_text("dict") will report back on
    re-extraction, a few points shorter (see Round 5 Finding 3). Use
    `tight_height`, not `box_height`, for anything that becomes an anchor
    the reflow chain measures a *gap* against later -- box_height there
    compounds every reformat-only pass, forever.

    Cached: `place()` calls this once per paragraph, and
    fit_placements_to_page's rescale loop re-measures *every* flowing
    paragraph on *every* scale step it tries (up to 8, from 1.0 down to
    MIN_FIT_SCALE in 0.04 steps) -- the same (width, text, fontsize)
    triples recur constantly across that loop and across reformat-only
    mode's paragraph-by-paragraph pass, where measurement is pure overhead
    with no model call to dominate it. Rounding width/fontsize/indent/
    leading to a fixed precision before hitting the cache trades a little
    measurement precision (well under a point) for a much higher hit rate
    than exact float equality would give."""
    return _measure_height_cached(
        round(width, 1), text, round(fontsize, 2), single_line, round(indent, 1),
        round(leading, 3) if leading else None,
    )


def fit_and_insert(page, rect, text, fontsize, single_line=False, indent=0.0, leading=None,
                    report=None):
    """Insert text (with '*italic*' markup) into rect. The box height was
    already sized to fit via measure_height, so scale_low=0 is just a
    safety net against small rounding differences between the two calls --
    with scale_low=0, MuPDF is free to shrink the text as far as it needs
    to make it fit rather than reporting a failure, so a real disagreement
    between measure_height's scratch-page measurement and this actual
    render (a leading/font-fallback/cache-key discrepancy) would otherwise
    show up only as a visually subtle shrunken paragraph -- invisible to
    every warning path Rounds 1-5 built. insert_htmlbox's return is
    (spare_height, scale); report()ing when scale is below 1.0 turns that
    entire class of future measure/render regressions grep-able instead of
    silent, the same argument Round 4 Finding 1 made for report() itself."""
    archive, css = font_setup()
    _spare, scale = page.insert_htmlbox(
        rect, paragraph_html(text, fontsize, single_line, indent, leading),
        css=css, archive=archive, scale_low=0,
    )
    if report and scale < 0.999:
        report(f"  WARNING: paragraph rendered at {scale:.2f}x -- "
               "measure_height disagreed with the actual render")


MIN_PARA_GAP = 2.0      # smallest gap we will squeeze a paragraph gap down to
MIN_FIT_SCALE = 0.72    # smallest font scale before we give up and warn


MAX_OBSTACLE_JUMPS = 32  # loop guard for the push-past-obstacle walk


def _clear_of_obstacles(y, h, x0, x1, obstacles, limit):
    """Lowest y >= the requested y at which an [x0,x1] x [y,y+h] box clears
    every obstacle whose x-range it overlaps.

    An obstacle only blocks a paragraph it actually sits beside. A figure in
    the left column must not push the right column's text down, so the x
    overlap test is required, not just the y one.
    """
    for _ in range(MAX_OBSTACLE_JUMPS):
        moved = False
        for ob in obstacles:
            r = ob["rect"]
            if r.x1 <= x0 or r.x0 >= x1:
                continue
            if y < r.y1 and (y + h) > r.y0:
                y = r.y1 + MIN_PARA_GAP
                moved = True
        if not moved:
            return y
        if y + h > limit:
            return y  # let the caller's overflow handling deal with it
    return y


def fit_placements_to_region(placements, region_rect, page_rect, obstacles=(),
                              bottom_margin=18.0, report=None):
    """Squeeze one region's flowing placements back inside that region.

    Was fit_placements_to_page (see fit_placements_to_page below for the
    original single-page-as-one-region docstring, still accurate for the
    common case). The two things this version adds:

    - The vertical limit comes from the *region*, not the page -- capped at
      the page's own bottom margin either way, since a region can't extend
      past the physical page regardless of its own rect. This matters most
      for columns: a column is roughly half the page's usable height per
      unit of text, so growth that a full-page limit absorbed silently now
      has to be fitted for real, and the gap-shrink-then-font-scale ladder
      fires far more often.
    - Placements route around `obstacles` (images, vector figures) via
      `_clear_of_obstacles` -- an obstacle in this region's x-range pushes
      a paragraph that would otherwise overlap it straight down past it,
      the same way a pinned folio already couldn't be overlapped.

    Reflow preserves each paragraph's original gap to the next one exactly,
    which is right, but it has no notion of a region bottom on its own: if
    the English runs longer than the German, every paragraph below the
    growth is pushed down, and once a rect passes the bottom of the region,
    insert_htmlbox still reports a clean fit (it is only asked whether the
    text fits the *rect*) and happily draws off-page. The result is content
    that is silently invisible in the output PDF -- no error, no warning,
    and the overflow also lands on top of any pinned folio on its way down.

    Two stages, cheapest first:
      1. Shrink the inter-paragraph gaps (never below MIN_PARA_GAP).
      2. If that is not enough, scale every flowing paragraph's font down by
         a uniform factor and re-measure, so the region stays visually
         consistent rather than having one arbitrary paragraph shrink.

    Pinned placements (folios, running heads) are page-anchored and are left
    exactly where they are. If even MIN_FIT_SCALE will not fit, the text is
    laid out at that scale and a warning is reported -- clipped-but-flagged
    beats silently dropped.

    Returns True if content still overflows even after the minimum-scale
    layout (i.e. some text was actually clipped), False otherwise (nothing
    needed fitting, or fitting fully succeeded).
    """
    flowing = [p for p in placements if not p["pinned"]]
    if not flowing:
        return False
    pinned = [p for p in placements if p["pinned"]]

    natural_top = flowing[0]["rect"].y0
    # The region's own bottom, but never past the page's bottom margin --
    # a region can't extend past the physical page regardless of its rect.
    limit = min(region_rect.y1, page_rect.y1 - bottom_margin)
    # A pinned footer folio/running-head sitting below the body must act as
    # a hard floor -- the original bug here (this function's own docstring
    # named it) was computing `limit` purely from the page box, so the
    # reflowed body could grow straight through a folio sitting well above
    # the page's actual bottom margin (a US-Letter folio around y=760 vs.
    # a fixed limit of 774).
    pinned_below = [p["rect"].y0 for p in pinned if p["rect"].y0 > natural_top]
    if pinned_below:
        limit = min(limit, min(pinned_below) - MIN_PARA_GAP)

    # Symmetrically, a pinned running head sitting at/above the body's
    # natural start is the actual first thing on the page -- start flowing
    # content below it rather than at flowing[0]'s own (possibly
    # overlapping) position.
    pinned_above = [p["rect"].y1 for p in pinned if p["rect"].y1 <= natural_top + 1.0]
    top = max([natural_top] + [y + MIN_PARA_GAP for y in pinned_above])

    if max(p["rect"].y1 for p in flowing) <= limit:
        return False

    gaps = [
        flowing[i]["rect"].y0 - flowing[i - 1]["rect"].y1
        for i in range(1, len(flowing))
    ]

    def lay_out(scale):
        """Place the paragraphs top-down at `scale`, with gaps shrunk only as
        far as needed and routed around any obstacle in their way. Returns
        (rects, sizes, overflow)."""
        sizes = [p["size"] * scale for p in flowing]
        # Uses box_height (not tight_height -- see measure_height) since
        # these heights size the actual rects a rescaled page is inserted
        # with, and here that rect's own height *is* what determines the
        # next paragraph's y-offset within this same layout pass -- unlike
        # place()'s reflow chain, there's no separately-tracked "original
        # gap" input for a rescale to preserve, so there's nothing here for
        # a subsequent reformat-only pass to read back and re-inflate
        # (a page rescaled once should fit at scale 1.0 on the next pass
        # and not need rescaling again at all). A repeatedly-rescaled page
        # is a narrow edge case not covered by Round 5 Finding 3's fix.
        heights = [
            measure_height(p["rect"].width, p["text"], sz, p["single_line"],
                           p.get("indent", 0.0), p.get("leading"))[0]
            for p, sz in zip(flowing, sizes)
        ]
        need = sum(heights) + sum(max(g, MIN_PARA_GAP) for g in gaps)
        avail = limit - top
        slack = sum(max(0.0, g - MIN_PARA_GAP) for g in gaps)
        # how much of the original gap slack we can afford to keep
        keep = 1.0 if slack <= 0 else max(0.0, min(1.0, (avail - need) / slack + 1.0))
        rects, y = [], top
        for i, (p, h) in enumerate(zip(flowing, heights)):
            if i:
                g = gaps[i - 1]
                y += MIN_PARA_GAP + max(0.0, g - MIN_PARA_GAP) * keep
            if obstacles:
                y = _clear_of_obstacles(y, h, p["rect"].x0, p["rect"].x1, obstacles, limit)
            rects.append(fitz.Rect(p["rect"].x0, y, p["rect"].x1, y + h))
            y += h
        return rects, sizes, y - limit

    scale = 1.0
    while True:
        rects, sizes, overflow = lay_out(scale)
        if overflow <= 0 or scale <= MIN_FIT_SCALE:
            break
        scale = max(MIN_FIT_SCALE, scale - 0.04)

    if overflow > 0 and report:
        # "OVERFLOW" is a stable, grep-able prefix distinguishing a genuine
        # clipped-content case from the merely-rescaled one below -- the
        # watcher (or anyone else scripting around this) can key off it
        # without parsing the human-readable sentence.
        report(f"  OVERFLOW: region content overflows by {overflow:.0f}pt even at "
               f"{scale:.2f}x font scale -- some text may be clipped")
    elif scale < 1.0 and report:
        report(f"  region content rescaled to {scale:.2f}x to fit")

    for p, r, sz in zip(flowing, rects, sizes):
        p["rect"], p["size"] = r, sz
    return overflow > 0


def fit_placements_to_page(placements, page_rect, bottom_margin=18.0, report=None):
    """Back-compat wrapper: the whole page as one region, no obstacles.
    See fit_placements_to_region for the real implementation."""
    return fit_placements_to_region(placements, page_rect, page_rect, (),
                                     bottom_margin, report)


MIN_CHARS_PER_PAGE = 50  # below this, warn that the output may be near-empty


class UnsupportedInputError(ValueError):
    """A preflight failure that retrying will never fix (encrypted, no
    pages, no extractable text) -- as opposed to a transient failure (a
    flaky model load, a truncated in-progress copy). Callers that retry on
    failure (the Folder Action watcher) can catch this specifically to skip
    straight to their permanent-failure bucket instead of burning
    MAX_ATTEMPTS retries that will all fail identically."""


def preflight(doc, page_indices, in_path, report):
    """Raise early on inputs that would otherwise fail badly deep inside
    process_pdf: an encrypted PDF surfaces as a cryptic `ValueError:
    document closed or encrypted` on the first page.get_text(), and an
    image-only scan silently extracts zero paragraphs, redacts nothing, and
    saves the untouched German original under an "_en" name with no
    indication anything went wrong."""
    if doc.needs_pass:
        raise UnsupportedInputError(f"{in_path} is password-protected; decrypt it first")
    if doc.page_count == 0:
        raise UnsupportedInputError(f"{in_path} has no pages")
    indices = list(page_indices)
    chars = sum(len(doc[p].get_text("text").strip()) for p in indices)
    if chars == 0:
        raise UnsupportedInputError(
            f"{in_path} has no extractable text (image-only scan?) -- "
            "run OCR (e.g. ocrmypdf) first"
        )
    if chars < MIN_CHARS_PER_PAGE * len(indices):
        report(f"WARNING: {in_path} has very little extractable text "
               f"({chars} chars across {len(indices)} page(s)); output may be near-empty")


def build_region_placements(region_rect, paragraphs, page_body_size, is_page_furniture,
                             model, tokenizer, skip_translation=False, temp=DEFAULT_TEMP,
                             report=None, pno=0):
    """The place()/translate per-paragraph loop, scoped to one region
    (a page, or -- once layout.analyze_page() is wired in -- one column or
    band within a page).

    Kept as one function, with the reflow chain's state
    (prev_orig_y1/prev_new_y1/prev_was_small_text/prev_was_short) local to
    this call, rather than inlined at each region's call site -- carrying
    that state across a region boundary is exactly how a two-column page
    would get zipped/misplaced again in a subtler form: column 2's first
    paragraph would inherit column 1's last y and land off the bottom of
    the page. Each call to this function starts that chain fresh."""
    def report_line(line):
        if report:
            report(line)

    prev_orig_y1 = None
    prev_new_y1 = None
    prev_was_small_text = False
    prev_was_short = False
    placements = []

    def place(para, translated, single_line, pinned=False):
        nonlocal prev_orig_y1, prev_new_y1, prev_was_small_text, prev_was_short
        x0, x1 = para["bbox"].x0, para["bbox"].x1
        if single_line:
            # headings/bylines were extracted with a bbox tight around
            # the original (German) text; insert_htmlbox's font metrics
            # differ slightly from insert_textbox's, so give short
            # single-line fragments a little slack to avoid an
            # unwanted wrap when the translation is a touch wider.
            # The slack is added on the RIGHT only: these boxes are
            # left-aligned (single_line -> text-align:left), so
            # widening leftwards moved the text itself off the body
            # margin (a 14pt heading drifted 10.5pt left of the
            # column it should line up with). Clamped to the REGION's
            # right edge, not the page's -- once regions are real
            # columns, a heading in the left column must not widen
            # across the gutter into the right one.
            x1 = min(x1 + para["size"] * 1.5, region_rect.x1 - 2)
        width = x1 - x0
        size = para["size"]
        is_small_text = page_body_size > 0 and size < page_body_size * 0.9
        # A title/subtitle fragment can wrap to 2 lines, so single_line
        # alone under-matches -- real body prose runs much longer than
        # any heading fragment or citation-metadata line, so a short
        # paragraph length is a better proxy for "this is part of a
        # title block or short list, not body prose".
        is_short = len(translated) < 120
        is_tight_run = (prev_was_small_text and is_small_text) or (
            prev_was_short and is_short
        )
        indent = para.get("indent", 0.0)
        leading = para.get("leading")
        if pinned:
            # Keep its original y and skip the prev_orig_y1/prev_new_y1
            # chain entirely, so it neither inherits the body's
            # accumulated shift nor passes a bogus gap on to whatever
            # follows (deliberately returns before prev_was_small_text/
            # prev_was_short are updated -- a folio between two
            # footnote entries shouldn't break tight-run detection for
            # the entry after it).
            new_y0 = para["bbox"].y0
            box_h, _tight_h = measure_height(width, translated, size, single_line, indent, leading)
            placements.append({
                "rect": fitz.Rect(x0, new_y0, x1, new_y0 + box_h),
                "text": translated, "size": size, "indent": indent, "leading": leading,
                "single_line": single_line, "pinned": True,
            })
            return
        if prev_new_y1 is None:
            new_y0 = para["bbox"].y0  # first paragraph in the region: keep as-is
        elif skip_translation and is_tight_run:
            # Reformat-only mode re-reads a PDF this script already
            # produced, so "the original gap" here just means whatever
            # gap is already baked into the (possibly buggy) input --
            # for a run of footnote/reference entries (same small size
            # back to back) or title/subtitle fragments (each a single
            # line, back to back), that's exactly the oversized,
            # compounding gap this mode exists to fix, so it's
            # discarded in favor of a small constant instead of
            # preserved.
            new_y0 = prev_new_y1 + 3.0
        else:
            new_y0 = prev_new_y1 + (para["bbox"].y0 - prev_orig_y1)
        box_h, tight_h = measure_height(width, translated, size, single_line, indent, leading)
        insert_bbox = fitz.Rect(x0, new_y0, x1, new_y0 + box_h)
        prev_orig_y1 = para["bbox"].y1
        # Anchored to new_y0 + tight_h (the glyph-only extent), not
        # insert_bbox.y1 (the CSS line-box height, a few points
        # taller -- see measure_height). Round 5 Finding 3: using
        # the padded box height here planted a gap that a *later*
        # reformat-only pass's re-extraction would read back as
        # part of "the original gap" to the next paragraph and
        # preserve verbatim, plus add a fresh instance of the same
        # gap on top -- compounding every pass, forever (2
        # paragraphs drifted +3pt/pass, 8 paragraphs +21pt/pass).
        # Anchoring on the same tight geometry a real re-extraction
        # will actually see leaves nothing left to compound.
        # insert_bbox itself still uses the full box_h, so nothing
        # clips; the few points of line-box padding below the last
        # line just becomes harmless blank space before the next
        # paragraph's rect starts, well short of overlapping it.
        prev_new_y1 = new_y0 + tight_h
        prev_was_small_text = is_small_text
        prev_was_short = is_short
        placements.append({
            "rect": insert_bbox, "text": translated, "size": size, "indent": indent,
            "leading": leading, "single_line": single_line, "pinned": False,
        })

    for para in paragraphs:
        text = para["text"].strip()
        pinned = is_page_furniture(para, text)

        # Also catches "- 17 -", "[17]", en-dash forms -- not just a bare
        # integer. `place()` must still be called or the page number is
        # simply erased with nothing drawn back in its place. Pinning
        # itself is decided by is_page_furniture() above, independent of
        # this regex -- this branch's only remaining job is "don't send a
        # bare number to the translator."
        if re.fullmatch(r"[\[\(]?\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*[\]\)]?", text):
            place(para, text, para["single_line"], pinned=pinned)
            continue

        if skip_translation:
            place(para, text, para["single_line"], pinned=pinned)
            continue

        # A paragraph with almost no real text to translate (e.g. just
        # a footnote marker, a stray dash) isn't a meaningful translation
        # request -- sending it to the model risks getting back a
        # confused conversational reply ("please provide the text...")
        # instead of a translation, which would then get inserted into
        # the PDF as if it were real content. Safer to leave it as-is.
        core = FOOTNOTE_MARKER_RE.sub("", text).replace("*", "").strip()
        if len(core) < 4:
            report_line(f"  page {pno + 1}: skipped near-empty paragraph "
                        f"[{len(text)} chars] -> skipped (too little content)")
            place(para, text, para["single_line"], pinned=pinned)
            continue

        translated = translate(model, tokenizer, text, temp=temp, report=report)
        reason = bad_translation_reason(text, translated)
        if reason and temp != 0.0:
            # Only worth a retry when the first attempt wasn't
            # already deterministic (temp=0.0) -- retrying at the
            # same temp/seed would just reproduce the same output.
            report_line(f"  page {pno + 1}: {reason}; retrying once at temp=0")
            retried = translate(model, tokenizer, text, temp=0.0, report=report)
            if not bad_translation_reason(text, retried):
                translated = retried
                reason = None
        if reason:
            report_line(f"  page {pno + 1}: WARNING: {reason} "
                        f"(paragraph: {text[:60]!r})")
        translated = preserve_footnote_markers(text, translated)
        report_line(f"  page {pno + 1}: paragraph translated "
                    f"[{len(text)} chars] -> [{len(translated)} chars]")
        place(para, translated, para["single_line"], pinned=pinned)

    return placements


_NON_TEXT_CELL_RE = re.compile(
    r"^[\s\d\.,;:%°/\-+×x()\[\]§–—]*$"
)
CELL_INSET = 1.0        # pt; keeps the redaction off the drawn border
CELL_MIN_SCALE = 0.60   # cells cannot grow, so they shrink further than prose


def cell_font(page, rect):
    """(size, single_line) read from the spans actually inside `rect`."""
    d = page.get_text("dict", clip=rect)
    lines = [l for b in d["blocks"] if b["type"] == 0
             for l in b["lines"] if l["spans"]]
    if not lines:
        return FALLBACK_FONT_SIZE, True
    return dominant_size(lines), len(lines) == 1


def fit_cell_size(rect, text, size):
    """Largest size <= `size` at which `text` fits `rect`, down to
    CELL_MIN_SCALE.

    A cell has a fixed height -- unlike prose, it cannot push the row below
    it down without breaking the drawn grid -- so overflow has to be
    absorbed by type size alone. English is usually only mildly longer
    than German, but a two-word German compound rendered as a five-word
    English phrase in a narrow column is exactly the case that needs this.
    """
    inner_w = rect.width - 2 * CELL_INSET
    inner_h = rect.height - 2 * CELL_INSET
    s = size
    while s > size * CELL_MIN_SCALE:
        box_h, _tight_h = measure_height(inner_w, text, s, True, 0.0)
        if box_h <= inner_h:
            return s
        s -= 0.25
    return size * CELL_MIN_SCALE


def process_table_region(page, table, model, tokenizer, temp=DEFAULT_TEMP,
                          report=None):
    """Translate a table cell by cell.

    Cell by cell rather than paragraph by paragraph because the grid is the
    content: a table's rows read left to right, but the paragraph splitter
    reads a page top to bottom and would concatenate "Jahr | Auflage |
    Preis" with the first data row into one sentence-shaped string. It also
    means each cell's rect is known exactly, so the translation goes back
    where it came from with no reflow at all.

    A short cell is the degenerate translation request bad_translation_reason
    exists to catch -- a two-word cell gives the model almost no context and
    invites a conversational reply -- so numeric/symbolic cells are never
    sent, and any cell whose response fails the plausibility check keeps its
    source text rather than the model's output.
    """
    placements = []
    redactions = []
    data = table.extract()

    for row_idx, row in enumerate(table.rows):
        for col_idx, cell_bbox in enumerate(row.cells):
            if cell_bbox is None:
                continue
            try:
                source = (data[row_idx][col_idx] or "").strip()
            except IndexError:
                continue
            if not source:
                continue

            rect = fitz.Rect(cell_bbox)
            inner = fitz.Rect(rect.x0 + CELL_INSET, rect.y0 + CELL_INSET,
                               rect.x1 - CELL_INSET, rect.y1 - CELL_INSET)
            if inner.is_empty:
                continue

            source = re.sub(r"\s+", " ", source)
            if _NON_TEXT_CELL_RE.match(source) or len(source) < 2:
                continue  # a number, a unit, a dash: leave it untouched

            translated = translate(model, tokenizer, source, temp=temp,
                                    report=report)
            reason = bad_translation_reason(source, translated)
            if reason:
                if report:
                    report(f"    cell r{row_idx}c{col_idx}: {reason} "
                           f"-- keeping source text")
                continue

            size, single_line = cell_font(page, rect)
            size = fit_cell_size(inner, translated, size)
            placements.append({
                "rect": inner, "text": translated, "size": size,
                "indent": 0.0, "leading": None, "single_line": True, "pinned": True,
            })
            redactions.append(inner)

    if report:
        report(f"  table: {len(placements)} cell(s) translated")
    return placements, redactions


def apply_text_redactions(page, paragraph_rects, placements, obstacles=()):
    """Erase the source text, one rect per paragraph rather than one union
    rect for the page.

    A single union rect was fine for single-column prose but is wrong as
    soon as anything sits inside the text column's bounding box: the union
    spans around a mid-column figure, and add_redact_annot's fill=(1,1,1)
    paints white over that whole rect -- images=PDF_REDACT_IMAGE_NONE stops
    the image being *removed* by the redaction, but does not stop a white
    rectangle being drawn on top of it. Per-paragraph rects never cover a
    figure in the first place, and on a multi-column page they also stop
    one column's redaction from wiping the other's text before it has been
    re-inserted.
    """
    rects = [fitz.Rect(r) for r in paragraph_rects]
    rects += [fitz.Rect(p["rect"]) for p in placements]

    for r in rects:
        r = fitz.Rect(r)
        r.x0 -= 1
        r.y0 -= 1
        r.x1 += 1
        r.y1 += 1
        r &= page.rect
        if r.is_empty:
            continue
        if any(ob["rect"].intersects(r) for ob in obstacles):
            # Never redact through a figure. Clipping the rect to avoid it
            # is possible but rarely needed: reflow has already routed text
            # around obstacles, so an intersection here means a stray span
            # (an axis label extracted as text), which is better left alone
            # than half-erased.
            continue
        page.add_redact_annot(r, fill=(1, 1, 1))

    if rects:
        # Only the text is being replaced. The default
        # (PDF_REDACT_IMAGE_PIXELS) blanks any image intersecting the
        # rect -- every figure/plate/scan on the page would otherwise be
        # erased even where its rect wasn't skipped above.
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )


def process_pdf(in_path, out_path, model_name, page_range=None, progress_callback=None,
                 skip_translation=False, force=False, temp=DEFAULT_TEMP, seed=DEFAULT_SEED,
                 use_tables=True, table_strategy="lines"):
    """progress_callback(str), if given, is called with a short status line
    at the start of each page and after each paragraph is translated -- the
    watcher script uses this to write live progress to a log file, since
    print() from a Folder-Action-triggered process isn't visible anywhere.

    skip_translation=True re-extracts and re-lays-out an *already-English*
    PDF (e.g. one this script produced earlier) without calling the model
    again -- for reformatting/layout fixes where re-translating would be
    wasteful or would introduce fresh non-determinism into text that's
    already correct.

    force is now a deprecated no-op, kept only so an existing Folder Action
    invocation or script passing it doesn't break: the page is region-aware
    now (see layout.analyze_page), so a multi-column page is translated
    correctly rather than needing to be either skipped or forced through
    scrambled.

    use_tables/table_strategy are passed straight to layout.find_tables;
    see --no-tables/--table-strategy's CLI help."""
    def report(line):
        # Always visible on stderr -- this is the only place a real overflow
        # warning (fit_placements_to_page) or a skipped-near-empty-paragraph
        # notice is emitted, and the plain `translate_pdf.py in.pdf out.pdf`
        # CLI invocation passes no progress_callback at all. Before this fix
        # those reports were computed, formatted, and silently thrown away on
        # every invocation except the Folder Action watcher's.
        print(line, file=sys.stderr)
        if progress_callback:
            progress_callback(line)

    doc = fitz.open(in_path)
    try:
        page_indices = range(len(doc)) if page_range is None else page_range
        total_pages = len(page_indices) if hasattr(page_indices, "__len__") else len(list(page_indices))

        # Fail fast, before the ~2GB model load (minutes, plus a possible
        # cold-cache download): an encrypted PDF or an image-only scan both
        # produce a bad outcome *after* paying that cost otherwise -- the
        # former as a cryptic ValueError from the first page.get_text(), the
        # latter as a silent no-op (zero paragraphs extracted, nothing
        # redacted, the German original saved verbatim under an "_en" name
        # with no indication anything went wrong). Still worth running in
        # --reformat-only mode too (no model load to save there, but the
        # same two failure modes apply to whatever's being reformatted).
        preflight(doc, page_indices, in_path, report)

        model, tokenizer = (None, None) if skip_translation else load_model(model_name, seed=seed)

        for i, pno in enumerate(page_indices, start=1):
            page = doc[pno]

            plan = layout.analyze_page(page, use_tables=use_tables,
                                        table_strategy=table_strategy)
            obstacles = plan["obstacles"]
            report(f"--- page {pno + 1} ({i}/{total_pages}) --- "
                   f"{len(plan['regions'])} region(s), "
                   f"{plan['columns']} column(s), "
                   f"{len(obstacles)} obstacle(s), "
                   f"{len(plan['tables'])} table(s)")

            # Font size stays page-wide (not per-region) for footnote-marker
            # detection specifically (span_is_footnote_marker, via
            # split_lines_into_paragraphs' dominant_size_override) -- a
            # narrow column of nothing but footnotes would otherwise decide
            # its own small type *is* the body size and stop recognizing
            # its own markers.
            all_free_lines = [
                l for r in plan["regions"] if r["kind"] == "text" for l in r["lines"]
            ]
            page_dom_size = dominant_size(all_free_lines) if all_free_lines else None

            # Page-anchored furniture (folios, running heads) lives in the
            # top/bottom margin band and must NOT ride the reflow chain --
            # a folio is anchored to the *page*, not to the text flow, so
            # letting it inherit the body's accumulated shift moves it
            # (sometimes hundreds of points) away from where it belongs. See
            # the `pinned` branch in build_region_placements()'s place().
            page_h = page.rect.height
            header_band = page_h * 0.12
            footer_band = page_h * 0.88

            def is_page_furniture(para, text):
                """True if `para` is page-anchored furniture (a folio, a
                textual running head/footer) rather than body prose.

                Pinning used to be reachable only through the bare-page-
                number regex below, so a *textual* running head or footer
                ("Kapitel 3 - Einleitung", "Zeitschrift fuer
                Sozialforschung, Jg. 12" -- the norm in academic German
                typesetting) rode the reflow chain like ordinary body text
                and inherited the body's accumulated shift: exactly the
                Round 2 regression, just for a class of furniture that fix
                didn't cover. Pinning is decided on geometry instead, for
                every paragraph, not just digit-only ones.

                Guarded by single-line-and-short as well as in-band, so a
                short document whose body legitimately starts inside the
                12% header band doesn't get its own first paragraph pinned
                by mistake.
                """
                in_band = para["bbox"].y1 <= header_band or para["bbox"].y0 >= footer_band
                return in_band and para["single_line"] and len(text) < 80

            all_placements = []
            redact_rects = []

            for region in plan["regions"]:
                if region["kind"] == "table":
                    if skip_translation:
                        continue  # cells were already rendered in place
                    tp, tr = process_table_region(
                        page, region["table"], model, tokenizer,
                        temp=temp, report=report,
                    )
                    all_placements.extend(tp)
                    redact_rects.extend(tr)
                    continue

                # Paragraphs are detected within this region's own lines
                # (see split_lines_into_paragraphs) -- reflow is likewise
                # scoped to the region (see build_region_placements /
                # fit_placements_to_region), so a paragraph that grows or
                # shrinks pushes everything below it up or down within its
                # own column, not the whole page and not the column beside it.
                paragraphs = [
                    p for p in split_lines_into_paragraphs(
                        region["lines"], dominant_size_override=page_dom_size,
                        dehyphenate=not skip_translation,
                    )
                    if p["text"].strip()
                ]
                if not paragraphs:
                    continue

                # Per region, not per page: on a two-column page the body
                # size must be read from the column being laid out, or a
                # column of footnotes beside a column of body text gets the
                # body-text column's size and its whole tight-run detection
                # inverted. Same reasoning as Round 3/4's page_body_size,
                # just scoped down from "page" to "region" now that a region
                # can be narrower than the page.
                sizes = [p["size"] for p in paragraphs]
                region_body_size = max(sizes) if sizes else 0

                # build_region_placements only uses this rect's x1 to cap a
                # single-line paragraph's right-edge slack (so a heading in
                # one column can't widen across the gutter into the next).
                # layout.analyze_page()'s own region rect is a *tight* bbox
                # around whatever content is actually there, recomputed
                # fresh on every re-extraction -- feeding that back in here
                # makes each reformat-only pass's output geometry a (very
                # slightly) different input to the next pass's tight-bbox
                # recomputation, and that feedback loop was enough on its
                # own to break Round 5 Finding 3's idempotency fix again,
                # by a small but non-plateauing amount pass over pass. On a
                # single-column page (the common case, and the one that
                # must not regress) there's no gutter to protect against in
                # the first place, so use the page's own stable rect
                # instead, exactly matching pre-refactor behavior with no
                # feedback loop at all. A genuinely multi-column page still
                # gets real gutter protection from the region rect.
                slack_rect = region["rect"] if plan["columns"] > 1 else page.rect
                placements = build_region_placements(
                    slack_rect, paragraphs, region_body_size, is_page_furniture,
                    model, tokenizer, skip_translation=skip_translation, temp=temp,
                    report=report, pno=pno,
                )
                # layout.analyze_page() gives each text region a *tight*
                # rect -- the bounding box of the content actually found
                # there, not the space available for it to grow into. Used
                # as-is for the fit limit, that turns any growth at all
                # (even a sub-point rounding difference between reformat
                # passes) into an "overflow" needing a rescale, which a
                # single-column page never needed before this refactor and
                # which also broke reformat-only idempotency (Round 5
                # Finding 3) by giving each pass a slightly different
                # rescale to chase. The real ceiling is wherever the next
                # region/table that overlaps this one's x-range starts, or
                # the page's own bottom margin if nothing does -- exactly
                # how the old single-region-per-page behavior already
                # worked, generalized to "the next thing in the way."
                growth_ceiling = min(
                    [other["rect"].y0 for other in plan["regions"]
                     if other is not region and other["rect"].y0 > region["rect"].y0
                     and not (other["rect"].x1 <= region["rect"].x0
                              or other["rect"].x0 >= region["rect"].x1)]
                    + [page.rect.y1]
                )
                fit_rect = fitz.Rect(region["rect"].x0, region["rect"].y0,
                                      region["rect"].x1, growth_ceiling)
                fit_placements_to_region(placements, fit_rect, page.rect,
                                          obstacles, report=report)
                all_placements.extend(placements)
                redact_rects.extend(fitz.Rect(p["bbox"]) for p in paragraphs)

            apply_text_redactions(page, redact_rects, all_placements, obstacles)

            for p in all_placements:
                fit_and_insert(page, p["rect"], p["text"], p["size"], p["single_line"],
                               p.get("indent", 0.0), p.get("leading"), report)

        # Document-level metadata (title/subject) is otherwise left entirely
        # untouched -- the body text gets translated but the PDF's own
        # /Title stays German, which shows up in the OS file-preview pane,
        # browser tab titles, and reference managers. Only the title is
        # worth the extra model call; /Subject and /Keywords are rarely
        # populated and not worth a second translation request per document.
        if not skip_translation and doc.metadata.get("title", "").strip():
            title = doc.metadata["title"].strip()
            try:
                translated_title = translate(model, tokenizer, title, temp=temp, report=report)
                if not bad_translation_reason(title, translated_title):
                    meta = dict(doc.metadata)
                    meta["title"] = translated_title
                    doc.set_metadata(meta)
                    report(f"  translated document title: {title!r} -> {translated_title!r}")
            except Exception as e:
                report(f"  WARNING: could not translate document title: {e}")

        # Each insert_htmlbox call embeds its own font subset rather than
        # reusing one already embedded on the page -- across a whole document
        # that's hundreds of duplicate font copies (e.g. 41 pages produced 1143
        # embedded font objects, ballooning a document that should be a few MB
        # into ~950MB). garbage=4 finds and merges/drops these duplicates.
        # A leading dot on the *filename* (not just a ".part" suffix) so a
        # watcher folder-action that fires on "item added" doesn't treat
        # this temp file as a new drop -- ".Bericht_en.pdf.part", not
        # "Bericht_en.pdf.part", which Finder/Folder Actions still surface
        # like any other visible file.
        out_path_obj = Path(out_path)
        tmp_out = str(out_path_obj.with_name(f".{out_path_obj.name}.part"))
        doc.save(tmp_out, garbage=4, deflate=True)
    finally:
        doc.close()
    os.replace(tmp_out, out_path)
    print(f"Saved {out_path}", file=sys.stderr)


def parse_page_range(spec, npages, warn=None):
    """warn(str), if given, is called once per --pages segment that selects
    no pages in this document (e.g. "99" on a 5-page doc) -- previously
    silent, since only the *combined* result across all segments was
    checked for emptiness, so a typo in one segment of a multi-segment spec
    ("1-3,99") was masked by the other, valid segments."""
    if not spec:
        return None
    result = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            raise ValueError(f"bad --pages segment {part!r}; expected e.g. 1-5 or 1,3,5")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a < 1 or b < a:
            raise ValueError(f"bad --pages range {part!r}; pages are 1-based and ascending")
        segment_pages = [p for p in range(a - 1, b) if 0 <= p < npages]
        if not segment_pages and warn:
            warn(f"--pages segment {part!r} selects no pages in this "
                 f"{npages}-page document (typo?)")
        result.extend(segment_pages)
    pages = sorted(set(result))
    if not pages:
        raise ValueError(f"--pages {spec!r} selects no pages in a {npages}-page document")
    return pages


def check_pdf(in_path, page_range=None, report=print, use_tables=True,
              table_strategy="lines"):
    """Report per-page extraction/layout diagnostics for `in_path` without
    loading the model or translating anything -- lets you sanity-check how
    the heuristics in layout.analyze_page/split_lines_into_paragraphs are
    reading an unfamiliar document (garbled paragraph counts, a
    suspiciously large near-empty count, a modal margin that doesn't match
    the visible body text, an unexpected column/table/obstacle count)
    before committing to a full, slow, model-backed run. `report` receives
    each output line (print by default; tests pass something else to
    capture it).

    Region-aware since the layout-support work: paragraph/body-size/
    near-empty stats are summed/computed across all of a page's *text*
    regions (columns, bands), and region/obstacle/table counts are
    reported alongside. A single-column, figure-free, table-free page
    (the common case) reports exactly what the pre-layout-support version
    of this function did, just phrased in terms of "1 region" instead of
    implicitly assuming one.

    Returns the list of per-page stat dicts, for programmatic use (e.g. the
    golden-file regression test asserts against these instead of scraping
    printed text).
    """
    doc = fitz.open(in_path)
    try:
        page_indices = range(len(doc)) if page_range is None else page_range
        all_stats = []
        for pno in page_indices:
            page = doc[pno]
            plan = layout.analyze_page(page, use_tables=use_tables,
                                        table_strategy=table_strategy)

            all_paragraphs = []
            all_lines = []
            for region in plan["regions"]:
                if region["kind"] != "text":
                    continue
                all_lines.extend(region["lines"])
                all_paragraphs.extend(split_lines_into_paragraphs(region["lines"]))

            near_empty = 0
            for para in all_paragraphs:
                # Same "nothing real to translate" test process_pdf uses to
                # decide whether a paragraph is worth sending to the model.
                core = FOOTNOTE_MARKER_RE.sub("", para["text"]).replace("*", "").strip()
                if len(core) < 4:
                    near_empty += 1

            sizes = [p["size"] for p in all_paragraphs]
            stats = {
                "page": pno + 1,
                "paragraphs": len(all_paragraphs),
                "body_size": max(sizes) if sizes else None,
                "modal_margin": modal_left_margin(all_lines) if all_lines else None,
                "near_empty": near_empty,
                "regions": len(plan["regions"]),
                "columns": plan["columns"],
                "obstacles": len(plan["obstacles"]),
                "tables": len(plan["tables"]),
            }
            all_stats.append(stats)
            report(
                f"page {stats['page']}: {stats['paragraphs']} paragraph(s), "
                f"body size {stats['body_size']}, "
                f"modal left margin {stats['modal_margin']}, "
                f"{stats['near_empty']} near-empty, "
                f"{stats['regions']} region(s), {stats['columns']} column(s), "
                f"{stats['obstacles']} obstacle(s), {stats['tables']} table(s)"
            )
        return all_stats
    finally:
        doc.close()


def main(argv=None):
    """Wrapped in a real function (rather than living directly under the
    `if __name__ == "__main__":` guard) so the CLI itself is importable and
    testable, e.g. `main(["in.pdf", "out.pdf", "--check"])` from a test,
    rather than only invocable as a subprocess."""
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output", nargs="?", default=None,
                     help="required unless --check is given")
    ap.add_argument("--pages", default=None, help="e.g. 1-5 or 1,3,5")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--reformat-only", action="store_true",
                     help="re-lay-out an already-English PDF without translating "
                          "(see 'Reformat-only mode' in the README)")
    ap.add_argument("--check", action="store_true",
                     help="report paragraph count/detected body size/modal margin/"
                          "near-empty-paragraph count per page, without translating "
                          "or writing any output (see 'Sanity-checking a document' "
                          "in the README)")
    ap.add_argument("--force", action="store_true",
                     help="deprecated, now a no-op: multi-column pages are "
                          "translated properly (see 'Layout support' in the "
                          "README) and no longer need forcing through")
    ap.add_argument("--temp", type=float, default=DEFAULT_TEMP,
                     help=f"sampling temperature (default {DEFAULT_TEMP} = "
                          "deterministic; translation is a one-right-answer task, "
                          "so there's usually no reason to raise this)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                     help=f"RNG seed (default {DEFAULT_SEED}); makes even a "
                          "non-zero --temp repeatable run to run")
    ap.add_argument("--no-tables", action="store_true",
                     help="skip table detection; table text is then treated as "
                          "ordinary prose, which will scramble it (see 'Layout "
                          "support' in the README)")
    ap.add_argument("--table-strategy", default="lines",
                     choices=["lines", "lines_strict", "text"],
                     help="'lines' (default) needs drawn borders; 'text' also "
                          "finds borderless tables but false-positives on "
                          "reference lists and other short-paragraph runs")
    args = ap.parse_args(argv)

    try:
        doc = fitz.open(args.input)
        npages = len(doc)
        doc.close()
    except Exception as e:
        ap.error(f"can't open {args.input!r}: {e}")

    try:
        page_range = parse_page_range(args.pages, npages, warn=lambda w: print(w, file=sys.stderr))
    except ValueError as e:
        ap.error(str(e))

    if args.check:
        check_pdf(args.input, page_range, use_tables=not args.no_tables,
                  table_strategy=args.table_strategy)
        return

    if page_range is not None and len(page_range) < npages:
        # Only meaningful once we know we're actually about to
        # translate/reformat something -- printed unconditionally here
        # used to also fire for --check, where nothing is translated *or*
        # copied and the message was simply wrong.
        skipped = npages - len(page_range)
        if args.reformat_only:
            print(f"reformatting {len(page_range)} of {npages} page(s); "
                  f"the remaining {skipped} page(s) are copied through as-is",
                  file=sys.stderr)
        else:
            print(f"translating {len(page_range)} of {npages} page(s); "
                  f"the remaining {skipped} page(s) are copied through untranslated",
                  file=sys.stderr)

    if not args.output:
        ap.error("output is required unless --check is given")
    try:
        process_pdf(
            args.input, args.output,
            None if args.reformat_only else args.model,
            page_range, skip_translation=args.reformat_only,
            force=args.force, temp=args.temp, seed=args.seed,
            use_tables=not args.no_tables, table_strategy=args.table_strategy,
        )
    except UnsupportedInputError as e:
        ap.error(str(e))


if __name__ == "__main__":
    main()
