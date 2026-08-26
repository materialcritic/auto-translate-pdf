#!/usr/bin/env python3
"""
Layout-preserving German -> English PDF translator.

Usage:
    venv/bin/python translate_pdf.py input.pdf output.pdf [--pages 1-5] [--model mlx-community/translategemma-4b-it-4bit]

Approach:
  1. Extract text blocks per page with PyMuPDF, keeping bbox + font info.
  2. Split each block into paragraphs using first-line indentation as the
     paragraph-start signal (this is how justified academic body text is
     typically laid out: no blank line between paragraphs, just an indent).
  3. Translate each paragraph with TranslateGemma (run locally via mlx-lm),
     giving the model the previous paragraph as context for pronoun/verb
     continuity across the DE->EN word-order shift.
  4. Redact the original paragraph's bbox and re-insert the English text in
     the same box, shrinking font size if needed to fit (English is usually
     ~10% shorter than German, but not always).
"""

import argparse
import html
import re
import sys

import fitz  # PyMuPDF

DEFAULT_MODEL = "mlx-community/translategemma-4b-it-4bit"


def load_model(model_name):
    from mlx_lm import load
    print(f"Loading {model_name} ...", file=sys.stderr)
    model, tokenizer = load(model_name)
    # The tokenizer's default eos_token_id doesn't include <end_of_turn>,
    # which is what this chat template actually emits to end a response --
    # without this, generation runs to max_tokens every time.
    end_of_turn_ids = tokenizer.encode("<end_of_turn>", add_special_tokens=False)
    if len(end_of_turn_ids) == 1:
        tokenizer.eos_token_ids = set(tokenizer.eos_token_ids) | {end_of_turn_ids[0]}
    return model, tokenizer


def translate(model, tokenizer, german_text, prev_context=""):
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


def line_text_marking(line, dominant_size):
    """Like line_text, but with two kinds of markup injected as plain-text
    markers so they survive the trip through the translation model:

    - A span whose font is much smaller than the body text and consists
      only of digits (a footnote reference number set as a separate,
      smaller-sized span glued directly onto the preceding word) is
      rewritten as an explicit " [N] " token. Left as raw text, that marker
      would just get silently absorbed into the adjacent number/word (e.g.
      "1938" + "1" -> "19381") when spans are naively concatenated.
    - An italic span is wrapped in "*...*". TranslateGemma reliably passes
      this markdown-style emphasis through translation intact (verified
      empirically -- it already produces this style on its own for things
      like book titles), which plain italic formatting has no way to
      survive since the model only sees a text string.
    """
    parts = []
    for s in line["spans"]:
        t = s["text"]
        stripped = t.strip()
        if (
            stripped
            and s["size"] < dominant_size * 0.8
            and stripped.isdigit()
            and len(stripped) <= 3
        ):
            parts.append(f" [{stripped}] ")
        elif stripped and span_is_italic(s):
            lead = t[: len(t) - len(t.lstrip())]
            trail = t[len(t.rstrip()):]
            parts.append(f"{lead}*{stripped}*{trail}")
        else:
            parts.append(t)
    return "".join(parts)


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
    lines.sort(key=lambda l: l["bbox"][1])

    x0s = [round(l["bbox"][0], 1) for l in lines]
    margin = max(set(x0s), key=x0s.count)  # modal left margin = body text
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
            if not (s["size"] < page_dominant_size * 0.8 and s["text"].strip().isdigit())
        ]
        return dominant_size([{"spans": real_spans}]) if real_spans else None

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
        text = " ".join(
            line_text_marking(l, page_dominant_size).strip() for l in para_lines
        )
        if dehyphenate:
            # Only correct for re-joining a German word that a PDF's own
            # typesetting hyphenated across a line break -- never turn this
            # on when re-reading a PDF this script itself already rendered
            # (e.g. reformat-only mode), since insert_htmlbox never
            # hyphenates: any hyphen there is a real character (a compound
            # word, a name like "Schulte-Sasse", a page range "105-109"),
            # and blindly deleting it produces "SchulteSasse"/"105109".
            text = re.sub(r"-\*\s+\*", "", text)  # across an italic-run boundary first...
            text = re.sub(r"-\s+", "", text)  # ...then the plain case
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
        size = para_lines[0]["spans"][0]["size"] if para_lines[0]["spans"] else 10.0
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
    missing = [
        m for m in FOOTNOTE_MARKER_RE.findall(source_text)
        if f"[{m}]" not in translated_text
    ]
    if not missing:
        return translated_text
    return translated_text.rstrip() + " " + " ".join(f"[{m}]" for m in missing)


FONT_DIR = "/System/Library/Fonts/Supplemental"
FONT_ARCHIVE = fitz.Archive(FONT_DIR, "fonts")
BODY_CSS = """
    @font-face { font-family: body; src: url("fonts/Times New Roman.ttf"); }
    @font-face { font-family: body; font-style: italic;
                 src: url("fonts/Times New Roman Italic.ttf"); }
    * { font-family: body; margin: 0; padding: 0; hyphens: none; }
"""

ASTERISK_RUN_RE = re.compile(r"\*(.+?)\*")


def text_to_html(text):
    """Turn a string containing '*italic*' markers into an HTML fragment
    with <i> tags, escaping everything else. Falls back to plain (all
    asterisks stripped) if they're unbalanced -- e.g. the translation
    model dropped one -- rather than risk mis-nesting tags."""
    if text.count("*") % 2 != 0:
        text = text.replace("*", "")
        return html.escape(text)

    out = []
    pos = 0
    for m in ASTERISK_RUN_RE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append(f"<i>{html.escape(m.group(1))}</i>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def paragraph_html(text, fontsize, single_line=False):
    body = text_to_html(text)
    align = "left" if single_line else "justify"
    return f'<p style="font-size:{fontsize}pt; text-align:{align};">{body}</p>'


def measure_height(width, text, fontsize, single_line=False):
    """Height (pt) that `text` actually needs at `fontsize` in a box of
    given width, measured on a throwaway scratch page."""
    scratch = fitz.open()
    spage = scratch.new_page(width=width + 1, height=3000)
    rect = fitz.Rect(0, 0, width, 3000)
    spare_height, _ = spage.insert_htmlbox(
        rect, paragraph_html(text, fontsize, single_line),
        css=BODY_CSS, archive=FONT_ARCHIVE,
    )
    used = 3000 if spare_height < 0 else 3000 - spare_height
    scratch.close()
    return used


def fit_and_insert(page, rect, text, fontsize, single_line=False):
    """Insert text (with '*italic*' markup) into rect. The box height was
    already sized to fit via measure_height, so scale_low=0 is just a
    safety net against small rounding differences between the two calls."""
    page.insert_htmlbox(
        rect, paragraph_html(text, fontsize, single_line),
        css=BODY_CSS, archive=FONT_ARCHIVE, scale_low=0,
    )


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

        prev_context = ""
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

        prev_orig_y1 = None
        prev_new_y1 = None
        prev_was_small_text = False
        prev_was_short = False
        placements = []

        def place(para, translated, single_line):
            nonlocal prev_orig_y1, prev_new_y1, prev_was_small_text, prev_was_short
            x0, x1 = para["bbox"].x0, para["bbox"].x1
            if single_line:
                # headings/bylines were extracted with a bbox tight around
                # the original (German) text; insert_htmlbox's font metrics
                # differ slightly from insert_textbox's, so give short
                # single-line fragments a little slack to avoid an
                # unwanted wrap when the translation is a touch wider
                pad = para["size"] * 1.5
                x0, x1 = x0 - pad / 2, x1 + pad / 2
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
            placements.append((insert_bbox, translated, size, single_line))

        for para in paragraphs:
            text = para["text"].strip()
            if re.fullmatch(r"\d{1,4}", text):
                continue  # leave bare page-number lines untranslated

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
                prev_context = text
                continue

            translated = translate(model, tokenizer, text, prev_context)
            translated = preserve_footnote_markers(text, translated)
            prev_context = text
            print(f"  [{len(text)} chars] -> [{len(translated)} chars]", file=sys.stderr)
            report(f"  page {pno + 1}: paragraph translated ({len(text)} -> {len(translated)} chars)")
            place(para, translated, para["single_line"])

        if page_redact_bbox is not None:
            # cover the whole original text footprint, plus any net growth,
            # in one shot so no leftover German can peek through a shift
            if placements:
                last_bottom = max(p[0].y1 for p in placements)
                page_redact_bbox.y1 = max(page_redact_bbox.y1, last_bottom + 2)
            page.add_redact_annot(page_redact_bbox, fill=(1, 1, 1))
            page.apply_redactions()

        for insert_bbox, translated, size, single_line in placements:
            fit_and_insert(page, insert_bbox, translated, size, single_line)

    # Each insert_htmlbox call embeds its own font subset rather than
    # reusing one already embedded on the page -- across a whole document
    # that's hundreds of duplicate font copies (e.g. 41 pages produced 1143
    # embedded font objects, ballooning a document that should be a few MB
    # into ~950MB). garbage=4 finds and merges/drops these duplicates.
    doc.save(out_path, garbage=4, deflate=True)
    print(f"Saved {out_path}", file=sys.stderr)


def parse_page_range(spec, npages):
    if not spec:
        return None
    result = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            result.extend(range(int(a) - 1, int(b)))
        else:
            result.append(int(part) - 1)
    return [p for p in result if 0 <= p < npages]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--pages", default=None, help="e.g. 1-5 or 1,3,5")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    doc = fitz.open(args.input)
    npages = len(doc)
    doc.close()
    page_range = parse_page_range(args.pages, npages)

    process_pdf(args.input, args.output, args.model, page_range)
