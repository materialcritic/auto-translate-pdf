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
import html
import os
import re
import sys

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

DEFAULT_MODEL = "mlx-community/translategemma-4b-it-4bit"


def load_model(model_name):
    from mlx_lm import load
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


def translate(model, tokenizer, german_text):
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler

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
    sampler = make_sampler(temp=0.3)
    out = generate(
        model, tokenizer, prompt=prompt, max_tokens=1024, sampler=sampler, verbose=False
    )
    return out.strip()


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
        return 10.0
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
    the body text or written with Unicode superscript glyphs (which carry
    the superscripting in the characters themselves, so they're often set
    at full body size and would fail a size-only test). Returns the ASCII
    digit string, or None if this span isn't a marker."""
    digits = as_marker_digits(span["text"])
    if digits is None:
        return None
    if span["size"] < dominant_size * 0.8 or span["text"].strip() != digits:
        return digits
    return None


ASTERISK_SENTINEL = ""  # private use area; stands in for a literal "*"


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
            out = core + tail + nxt  # keep hyphen, drop the space
    return out


def modal_left_margin(lines):
    """The x0 shared by more lines than any other on the page -- i.e. the
    body-text left margin, used both to detect paragraph indentation here
    and as a `--check`-mode diagnostic (see check_pdf)."""
    x0s = [round(l["bbox"][0], 1) for l in lines]
    return max(set(x0s), key=x0s.count)


def split_page_into_paragraphs(blocks, left_margin_tolerance=4.0, gap_multiplier=1.7,
                                dehyphenate=True):
    """
    Group a page's lines into paragraphs using indentation as the primary
    signal: a line whose x0 is meaningfully greater than the page's modal
    left margin starts a new paragraph (standard academic-prose convention
    -- no blank line between paragraphs, just an indented first line).

    This works on ALL of a page's lines flattened together, not block by
    block. Some PDF producers group a whole paragraph's lines into one PDF
    "block" (then per-block indentation detection would be enough), but
    others emit one block per line -- in that case a block never contains
    more than one line to compare margins against, so indentation can never
    be detected and every line would be mistaken for its own paragraph,
    stripping context from mid-sentence line breaks and producing fragments
    the translator has no hope of rendering coherently. Flattening first
    makes the two cases indistinguishable, which is what we want.

    Two extra signals guard against merging things that just happen to
    share the body margin: a large jump in font size (heading dropped into
    the middle of body text) or an unusually large vertical gap (more than
    `gap_multiplier` line-heights) each also force a new paragraph.
    """
    lines = [l for b in blocks for l in b["lines"] if l["spans"]]
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
    page_dominant_size = dominant_size(lines)

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
        text = re.sub(r"\s+([.,;:!?])", r"\1", text)  # tidy space before punctuation
        bbox = fitz.Rect(para_lines[0]["bbox"])
        for l in para_lines[1:]:
            bbox |= fitz.Rect(l["bbox"])
        # The size covering the most characters in the whole paragraph, not
        # the first span's. A footnote/reference entry starts with its
        # superscript marker, so the first span is the ~6.5pt marker while
        # the entry itself is ~9pt -- taking the first span rendered whole
        # footnote blocks at marker size, and also skewed page_body_size
        # (which drives the small-text/tight-run checks in process_pdf).
        size = dominant_size(para_lines) if para_lines[0]["spans"] else 10.0
        font = para_lines[0]["spans"][0]["font"] if para_lines[0]["spans"] else "Times-Roman"
        # a paragraph that was a single line in the source is almost always
        # a heading/title/byline fragment, not real body prose -- justify
        # stretches those into ugly full-width letter-spacing if translation
        # makes them wrap, so they get left-aligned instead (see paragraph_html)
        single_line = len(para_lines) == 1
        result.append({
            "text": text, "bbox": bbox, "size": size, "font": font,
            "single_line": single_line,
        })
    return result


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
    asterisk to begin with) is dropped rather than risking mis-nested tags
    -- this used to be an all-or-nothing whole-string parity check, which
    meant one dropped asterisk anywhere killed italics for the entire
    paragraph."""
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
        # line_text_marking, and drop any leftover unmatched delimiter.
        return html.escape(chunk.replace("*", "").replace(ASTERISK_SENTINEL, "*"))

    out = []
    pos = 0
    for m in ASTERISK_RUN_RE.finditer(text):
        out.append(esc(text[pos:m.start()]))
        out.append(f"<i>{esc(m.group(1))}</i>")
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


def paragraph_html(text, fontsize, single_line=False):
    body = text_to_html(text)
    align = "left" if single_line else "justify"
    return f'<p style="font-size:{fontsize}pt; text-align:{align};">{body}</p>'


def measure_height(width, text, fontsize, single_line=False):
    """Height (pt) that `text` actually needs at `fontsize` in a box of
    given width, measured on a throwaway scratch page."""
    archive, css = font_setup()
    scratch = fitz.open()
    spage = scratch.new_page(width=width + 1, height=3000)
    rect = fitz.Rect(0, 0, width, 3000)
    spare_height, _ = spage.insert_htmlbox(
        rect, paragraph_html(text, fontsize, single_line),
        css=css, archive=archive,
    )
    used = 3000 if spare_height < 0 else 3000 - spare_height
    scratch.close()
    return used


def fit_and_insert(page, rect, text, fontsize, single_line=False):
    """Insert text (with '*italic*' markup) into rect. The box height was
    already sized to fit via measure_height, so scale_low=0 is just a
    safety net against small rounding differences between the two calls."""
    archive, css = font_setup()
    page.insert_htmlbox(
        rect, paragraph_html(text, fontsize, single_line),
        css=css, archive=archive, scale_low=0,
    )


MIN_PARA_GAP = 2.0      # smallest gap we will squeeze a paragraph gap down to
MIN_FIT_SCALE = 0.72    # smallest font scale before we give up and warn


def fit_placements_to_page(placements, page_rect, bottom_margin=18.0, report=None):
    """Squeeze a page's flowing placements back inside the page box.

    Reflow preserves each paragraph's original gap to the next one exactly,
    which is right, but it has no notion of a page bottom: if the English
    runs longer than the German, every paragraph below the growth is pushed
    down, and once a rect passes the bottom of the media box, insert_htmlbox
    still reports a clean fit (it is only asked whether the text fits the
    *rect*) and happily draws off-page. The result is content that is
    silently invisible in the output PDF -- no error, no warning, and the
    overflow also lands on top of any pinned folio on its way down.

    Two stages, cheapest first:
      1. Shrink the inter-paragraph gaps (never below MIN_PARA_GAP).
      2. If that is not enough, scale every flowing paragraph's font down by
         a uniform factor and re-measure, so the page stays visually
         consistent rather than having one arbitrary paragraph shrink.

    Pinned placements (folios, running heads) are page-anchored and are left
    exactly where they are. If even MIN_FIT_SCALE will not fit, the text is
    laid out at that scale and a warning is reported -- clipped-but-flagged
    beats silently dropped.
    """
    flowing = [p for p in placements if not p["pinned"]]
    if not flowing:
        return placements
    limit = page_rect.y1 - bottom_margin
    if max(p["rect"].y1 for p in flowing) <= limit:
        return placements

    top = flowing[0]["rect"].y0
    gaps = [
        flowing[i]["rect"].y0 - flowing[i - 1]["rect"].y1
        for i in range(1, len(flowing))
    ]

    def lay_out(scale):
        """Place the paragraphs top-down at `scale`, with gaps shrunk only as
        far as needed. Returns (rects, sizes, overflow)."""
        sizes = [p["size"] * scale for p in flowing]
        heights = [
            measure_height(p["rect"].width, p["text"], sz, p["single_line"]) + 2
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
        report(f"  WARNING: page content overflows by {overflow:.0f}pt even at "
               f"{scale:.2f}x font scale -- some text may be clipped")
    elif scale < 1.0 and report:
        report(f"  page content rescaled to {scale:.2f}x to fit the page")

    for p, r, sz in zip(flowing, rects, sizes):
        p["rect"], p["size"] = r, sz
    return placements


def process_pdf(in_path, out_path, model_name, page_range=None, progress_callback=None,
                 skip_translation=False):
    """progress_callback(str), if given, is called with a short status line
    at the start of each page and after each paragraph is translated -- the
    watcher script uses this to write live progress to a log file, since
    print() from a Folder-Action-triggered process isn't visible anywhere.

    skip_translation=True re-extracts and re-lays-out an *already-English*
    PDF (e.g. one this script produced earlier) without calling the model
    again -- for reformatting/layout fixes where re-translating would be
    wasteful or would introduce fresh non-determinism into text that's
    already correct."""
    def report(line):
        if progress_callback:
            progress_callback(line)

    doc = fitz.open(in_path)
    try:
        model, tokenizer = (None, None) if skip_translation else load_model(model_name)

        page_indices = range(len(doc)) if page_range is None else page_range
        total_pages = len(page_indices) if hasattr(page_indices, "__len__") else len(list(page_indices))

        for i, pno in enumerate(page_indices, start=1):
            page = doc[pno]
            print(f"--- page {pno + 1} ---", file=sys.stderr)
            report(f"page {pno + 1} ({i}/{total_pages})")
            d = page.get_text("dict")
            blocks = [b for b in d["blocks"] if b["type"] == 0]

            # paragraphs are detected across the whole page's lines at once (see
            # split_page_into_paragraphs) -- reflow is likewise page-wide, so a
            # paragraph that grows or shrinks pushes everything below it up or
            # down on the same page, not just its own original sibling lines
            paragraphs = []
            page_redact_bbox = None
            for para in split_page_into_paragraphs(blocks, dehyphenate=not skip_translation):
                if not para["text"].strip():
                    continue
                paragraphs.append(para)
                page_redact_bbox = (
                    fitz.Rect(para["bbox"]) if page_redact_bbox is None
                    else page_redact_bbox | para["bbox"]
                )

            # Tracks the *actual* (post-translation) bottom of the previous
            # paragraph, paired with that same paragraph's *original* bottom.
            # The gap before the next paragraph is then original_gap = next.y0
            # - prev_orig_y1, applied as new_y0 = prev_new_y1 + original_gap.
            # This preserves each paragraph's original spacing to the one after
            # it exactly, regardless of how much this paragraph itself grew or
            # shrank. The previous approach (accumulating every paragraph's own
            # height delta into a single running offset applied to each
            # paragraph's original y0) let a paragraph's shrinkage inflate the
            # gap *after* it -- harmless for a couple of paragraphs, but in a
            # references/footnotes list where every entry translates shorter
            # than the German, those inflated gaps compound down the page into
            # obviously-oversized blank space between every single entry.
            # Body text and footnote/reference-list text are reliably set at
            # different font sizes in the source document (e.g. 11.25pt body vs
            # 9pt footnotes) -- unlike a leading "[N]" marker, which often ends
            # up trailing the *previous* entry instead of leading its own (an
            # extraction quirk from how the source PDF positions footnote
            # numbers), a paragraph's font size survives untouched, so it's a
            # more reliable signal for "this is part of a tight reference list".
            # The largest size on the page, not the most common one -- a page
            # with more (short) footnote paragraphs than (long) body paragraphs
            # would otherwise make the footnote size "win" as the apparent body
            # size by paragraph count, even though it covers far less of the
            # page and is never actually the main text.
            page_sizes = [p["size"] for p in paragraphs]
            page_body_size = max(page_sizes) if page_sizes else 0

            # Page-anchored furniture (folios, running heads) lives in the
            # top/bottom margin band and must NOT ride the reflow chain --
            # a folio is anchored to the *page*, not to the text flow, so
            # letting it inherit the body's accumulated shift moves it
            # (sometimes hundreds of points) away from where it belongs. See
            # the `pinned` branch in place() below.
            page_h = page.rect.height
            header_band = page_h * 0.12
            footer_band = page_h * 0.88

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
                    # column it should line up with).
                    x1 = min(x1 + para["size"] * 1.5, page.rect.x1 - 2)
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
                if pinned:
                    # Keep its original y and skip the prev_orig_y1/prev_new_y1
                    # chain entirely, so it neither inherits the body's
                    # accumulated shift nor passes a bogus gap on to whatever
                    # follows (deliberately returns before prev_was_small_text/
                    # prev_was_short are updated -- a folio between two
                    # footnote entries shouldn't break tight-run detection for
                    # the entry after it).
                    new_y0 = para["bbox"].y0
                    needed_h = measure_height(width, translated, size, single_line)
                    placements.append({
                        "rect": fitz.Rect(x0, new_y0, x1, new_y0 + needed_h + 2),
                        "text": translated, "size": size,
                        "single_line": single_line, "pinned": True,
                    })
                    return
                if prev_new_y1 is None:
                    new_y0 = para["bbox"].y0  # first paragraph on the page: keep as-is
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
                needed_h = measure_height(width, translated, size, single_line)
                insert_bbox = fitz.Rect(x0, new_y0, x1, new_y0 + needed_h + 2)
                prev_orig_y1 = para["bbox"].y1
                prev_new_y1 = insert_bbox.y1
                prev_was_small_text = is_small_text
                prev_was_short = is_short
                placements.append({
                    "rect": insert_bbox, "text": translated, "size": size,
                    "single_line": single_line, "pinned": False,
                })

            for para in paragraphs:
                text = para["text"].strip()
                # Also catches "- 17 -", "[17]", en-dash forms -- not just a bare
                # integer. This paragraph's bbox is already folded into
                # page_redact_bbox above, so it WILL be redacted regardless;
                # `place()` must still be called or the page number is simply
                # erased with nothing drawn back in its place.
                if re.fullmatch(r"[\[\(]?\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*[\]\)]?", text):
                    in_margin_band = (
                        para["bbox"].y1 <= header_band or para["bbox"].y0 >= footer_band
                    )
                    place(para, text, para["single_line"], pinned=in_margin_band)
                    continue

                if skip_translation:
                    place(para, text, para["single_line"])
                    continue

                # A paragraph with almost no real text to translate (e.g. just
                # a footnote marker, a stray dash) isn't a meaningful translation
                # request -- sending it to the model risks getting back a
                # confused conversational reply ("please provide the text...")
                # instead of a translation, which would then get inserted into
                # the PDF as if it were real content. Safer to leave it as-is.
                core = FOOTNOTE_MARKER_RE.sub("", text).replace("*", "").strip()
                if len(core) < 4:
                    print(f"  [{len(text)} chars] -> skipped (too little content)", file=sys.stderr)
                    report(f"  page {pno + 1}: skipped near-empty paragraph")
                    place(para, text, para["single_line"])
                    continue

                translated = translate(model, tokenizer, text)
                translated = preserve_footnote_markers(text, translated)
                print(f"  [{len(text)} chars] -> [{len(translated)} chars]", file=sys.stderr)
                report(f"  page {pno + 1}: paragraph translated ({len(text)} -> {len(translated)} chars)")
                place(para, translated, para["single_line"])

            fit_placements_to_page(placements, page.rect, report=report)

            if page_redact_bbox is not None:
                # cover the whole original text footprint, plus any net growth,
                # in one shot so no leftover German can peek through a shift
                if placements:
                    last_bottom = max(p["rect"].y1 for p in placements)
                    page_redact_bbox.y1 = max(page_redact_bbox.y1, last_bottom + 2)
                page.add_redact_annot(page_redact_bbox, fill=(1, 1, 1))
                # Only the text is being replaced. The default
                # (PDF_REDACT_IMAGE_PIXELS) blanks any image intersecting the
                # rect, and this rect spans the whole text column -- every
                # figure/plate/scan on the page would otherwise be erased.
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                )

            for p in placements:
                fit_and_insert(page, p["rect"], p["text"], p["size"], p["single_line"])

        # Each insert_htmlbox call embeds its own font subset rather than
        # reusing one already embedded on the page -- across a whole document
        # that's hundreds of duplicate font copies (e.g. 41 pages produced 1143
        # embedded font objects, ballooning a document that should be a few MB
        # into ~950MB). garbage=4 finds and merges/drops these duplicates.
        tmp_out = str(out_path) + ".part"
        doc.save(tmp_out, garbage=4, deflate=True)
    finally:
        doc.close()
    os.replace(tmp_out, out_path)
    print(f"Saved {out_path}", file=sys.stderr)


def parse_page_range(spec, npages):
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
        result.extend(range(a - 1, b))
    pages = sorted({p for p in result if 0 <= p < npages})
    if not pages:
        raise ValueError(f"--pages {spec!r} selects no pages in a {npages}-page document")
    return pages


def check_pdf(in_path, page_range=None, report=print):
    """Report per-page extraction/paragraph-detection diagnostics for
    `in_path` without loading the model or translating anything -- lets you
    sanity-check how the heuristics in split_page_into_paragraphs are
    reading an unfamiliar document (garbled paragraph counts, a
    suspiciously large near-empty count, a modal margin that doesn't match
    the visible body text) before committing to a full, slow, model-backed
    run. `report` receives each output line (print by default; tests pass
    something else to capture it).

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
            d = page.get_text("dict")
            blocks = [b for b in d["blocks"] if b["type"] == 0]
            lines = [l for b in blocks for l in b["lines"] if l["spans"]]
            paragraphs = split_page_into_paragraphs(blocks)

            near_empty = 0
            for para in paragraphs:
                # Same "nothing real to translate" test process_pdf uses to
                # decide whether a paragraph is worth sending to the model.
                core = FOOTNOTE_MARKER_RE.sub("", para["text"]).replace("*", "").strip()
                if len(core) < 4:
                    near_empty += 1

            sizes = [p["size"] for p in paragraphs]
            stats = {
                "page": pno + 1,
                "paragraphs": len(paragraphs),
                "body_size": max(sizes) if sizes else None,
                "modal_margin": modal_left_margin(lines) if lines else None,
                "near_empty": near_empty,
            }
            all_stats.append(stats)
            report(
                f"page {stats['page']}: {stats['paragraphs']} paragraph(s), "
                f"body size {stats['body_size']}, "
                f"modal left margin {stats['modal_margin']}, "
                f"{stats['near_empty']} near-empty"
            )
        return all_stats
    finally:
        doc.close()


if __name__ == "__main__":
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
    args = ap.parse_args()

    doc = fitz.open(args.input)
    npages = len(doc)
    doc.close()
    try:
        page_range = parse_page_range(args.pages, npages)
    except ValueError as e:
        ap.error(str(e))

    if args.check:
        check_pdf(args.input, page_range)
    else:
        if not args.output:
            ap.error("output is required unless --check is given")
        process_pdf(
            args.input, args.output,
            None if args.reformat_only else args.model,
            page_range, skip_translation=args.reformat_only,
        )
