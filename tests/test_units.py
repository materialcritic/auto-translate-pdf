#!/usr/bin/env python3
"""Unit tests for the pure functions in translate_pdf.py -- no PDF, no
fixture, no model. These would have caught Round 4 Finding 5 directly
(join_paragraph_lines losing italics across a hyphenated line break):
that bug lived entirely inside one function's return value and needed no
PDF rendering to demonstrate, but nothing exercised it until an external
audit built a full rendering repro.

Plain asserts, no test framework dependency (none is in requirements.txt),
matching tests/test_golden.py's style. Run directly:
    ./venv/bin/python3 tests/test_units.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import translate_pdf as tp  # noqa: E402

failures = []


def check(name, cond, detail=None):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        print(f"       {detail!r}")
        failures.append(name)


# --- join_paragraph_lines: all four branches x {italic, non-italic} x
# --- {dehyphenate on, off} (Finding 5's own regression, plus the three
# --- sibling branches that were already correct) ---

JOIN_CASES = [
    # (pieces, dehyphenate) -> expected
    (["Schulte-", "Sasse, Literarische"], True, "Schulte-Sasse, Literarische"),
    (["Schulte-", "Sasse, Literarische"], False, "Schulte-Sasse, Literarische"),
    (["Auf-", "klaerung"], True, "Aufklaerung"),  # lowercase continuation -> fuse
    (["Auf-", "klaerung"], False, "Auf-klaerung"),
    (["S. 105-", "109."], True, "S. 105-109."),
    (["S. 105-", "109."], False, "S. 105-109."),
    (["Zur Sozial-", "und Wirtschaftsgeschichte."], True, "Zur Sozial- und Wirtschaftsgeschichte."),
    (["Zur Sozial-", "und Wirtschaftsgeschichte."], False, "Zur Sozial- und Wirtschaftsgeschichte."),
    (["kein Zeilenumbruch hier", "geht normal weiter"], True,
     "kein Zeilenumbruch hier geht normal weiter"),
    # Italic variants -- the Finding 5 regression itself (proper-noun/
    # page-range branch across an italic boundary) plus its three siblings.
    (["*Schulte-*", "*Sasse*"], True, "*Schulte-Sasse*"),  # Finding 5
    (["*Schulte-*", "*Sasse*"], False, "*Schulte-Sasse*"),
    (["*Titel-*", "*Fortsetzung*"], True, "*Titel-Fortsetzung*"),  # Finding 5, 2nd case
    (["*arbei-*", "*ten*"], True, "*arbeiten*"),  # lowercase-fuse sibling (was already fine)
]

for pieces, dehyph, expected in JOIN_CASES:
    got = tp.join_paragraph_lines(pieces, dehyph)
    check(f"join_paragraph_lines({pieces!r}, dehyphenate={dehyph})",
          got == expected, got)

# The suspension case with italics is awkward to spell as a one-liner above;
# check it directly instead of folding it into the table.
got = tp.join_paragraph_lines(["*Zur Sozial-*", "*und Wirtschaft*"], True)
check('join_paragraph_lines suspension keeps hyphen+space even across italics',
      got == "*Zur Sozial-* *und Wirtschaft*", got)

# --- text_to_html: italic runs, bold normalization, unmatched/literal
# --- asterisks (Finding 13's "keep, don't delete" change) ---

check('text_to_html: simple italic run',
      tp.text_to_html("das *Kapital* von Marx") == "das <i>Kapital</i> von Marx")
check('text_to_html: adjacent italic runs stay separate (Round 2 leftover #1)',
      tp.text_to_html("*Marx* *Engels*") == "<i>Marx</i> <i>Engels</i>")
check('text_to_html: **bold** normalizes to italic, not "*<i>...</i>"',
      tp.text_to_html("**Dialektik**") == "<i>Dialektik</i>")
check('text_to_html: fused italic across a hyphen (Finding 5 downstream)',
      tp.text_to_html("*Schulte-Sasse*") == "<i>Schulte-Sasse</i>")
check('text_to_html: literal asterisk on the sentinel survives as "*"',
      tp.text_to_html(f"geboren ({tp.ASTERISK_SENTINEL} 1903)") == "geboren (* 1903)")
check('text_to_html: unmatched model-emitted "*" renders literally, not deleted (Finding 13)',
      tp.text_to_html("2 * 4 = 8") == "2 * 4 = 8")
check('text_to_html: "**" bold markers normalize to single "*"s, which then '
      "render as literal characters since they don't pair up as a real "
      "run (whitespace-only content) -- Finding 13 means these are no "
      "longer silently deleted",
      tp.text_to_html("a **  ** b") == "a *  * b")

# --- strip_sentinel_for_model / restore_sentinel_from_model round trip
# --- (Finding 13, the "sentinel travels through the LLM" half) ---

s = f"Adorno ({tp.ASTERISK_SENTINEL} 1903) und {tp.ASTERISK_SENTINEL}Kapital{tp.ASTERISK_SENTINEL}"
stripped = tp.strip_sentinel_for_model(s)
check("strip_sentinel_for_model removes the private-use sentinel entirely",
      tp.ASTERISK_SENTINEL not in stripped, stripped)
check("restore_sentinel_from_model round-trips exactly",
      tp.restore_sentinel_from_model(stripped) == s, stripped)

# --- as_marker_digits / span_is_footnote_marker ---

check('as_marker_digits: ASCII digits pass through',
      tp.as_marker_digits("12") == "12")
check('as_marker_digits: superscript unicode normalizes to ASCII',
      tp.as_marker_digits("¹") == "1")  # superscript 1
check('as_marker_digits: non-digit text returns None',
      tp.as_marker_digits("Vgl.") is None)
check('as_marker_digits: too many digits (>3) returns None',
      tp.as_marker_digits("1234") is None)

small_marker = {"text": "1", "size": 6.5}
full_size_marker = {"text": "¹", "size": 9.0}  # superscript glyph, full body size
not_a_marker = {"text": "1938", "size": 9.0}
check("span_is_footnote_marker: small digit-only span is a marker",
      tp.span_is_footnote_marker(small_marker, 9.0) == "1")
check("span_is_footnote_marker: full-size superscript GLYPH is still a marker",
      tp.span_is_footnote_marker(full_size_marker, 9.0) == "1")
check("span_is_footnote_marker: full-size ASCII digits are NOT a marker",
      tp.span_is_footnote_marker(not_a_marker, 9.0) is None)

# --- preserve_footnote_markers ---

check("preserve_footnote_markers: nothing missing -> unchanged",
      tp.preserve_footnote_markers("Text [1] here.", "Text [1] here.") == "Text [1] here.")
check("preserve_footnote_markers: dropped marker gets appended",
      tp.preserve_footnote_markers("Text [1] here.", "Text here.") == "Text here. [1]")
check("preserve_footnote_markers: count-aware -- one survivor of a doubled [1] still appends one",
      tp.preserve_footnote_markers("[1] and [1] again.", "[1] and again.") == "[1] and again. [1]")

# --- bad_translation_reason (Finding 10) ---

check("bad_translation_reason: plausible translation -> None",
      tp.bad_translation_reason(
          "Der Autor beschreibt die Situation im Detail und erlaeutert vieles.",
          "The author describes the situation in detail and explains a lot.",
      ) is None)
check("bad_translation_reason: echoed source flagged",
      tp.bad_translation_reason(
          "Der Autor beschreibt die Situation im Detail und erlaeutert vieles heute.",
          "Der Autor beschreibt die Situation im Detail und erlaeutert vieles heute.",
      ) is not None)
check("bad_translation_reason: much-shorter output flagged (probable truncation)",
      tp.bad_translation_reason("x" * 100, "short") is not None)
check("bad_translation_reason: much-longer output flagged (probable runaway)",
      tp.bad_translation_reason("short text here", "x" * 200) is not None)
check("bad_translation_reason: conversational refusal flagged",
      tp.bad_translation_reason(
          "Ein kurzer Satz.",
          "Please provide the German text you would like translated.",
      ) is not None)

# --- parse_page_range ---

check("parse_page_range: None spec -> None (all pages)",
      tp.parse_page_range(None, 10) is None)
check("parse_page_range: single range",
      tp.parse_page_range("1-5", 10) == [0, 1, 2, 3, 4])
check("parse_page_range: comma-separated mix",
      tp.parse_page_range("1,3,5-6", 10) == [0, 2, 4, 5])
check("parse_page_range: out-of-range segment warns instead of silently vanishing (Finding 21)",
      (lambda warnings: (
          tp.parse_page_range("1-3,99", 5, warn=warnings.append) == [0, 1, 2]
          and len(warnings) == 1 and "99" in warnings[0]
      ))([]))
try:
    tp.parse_page_range("99", 5)
    check("parse_page_range: all-out-of-range spec raises", False)
except ValueError:
    check("parse_page_range: all-out-of-range spec raises", True)
try:
    tp.parse_page_range("abc", 5)
    check("parse_page_range: malformed segment raises", False)
except ValueError:
    check("parse_page_range: malformed segment raises", True)

# --- modal_left_margin ---

lines = [{"bbox": (72.0, 0, 0, 0)}, {"bbox": (72.0, 0, 0, 0)}, {"bbox": (90.0, 0, 0, 0)}]
check("modal_left_margin: most common x0 wins",
      tp.modal_left_margin(lines) == 72.0)

# --- detect_columns (Finding 8) ---

def fake_line(x0):
    return {"bbox": (x0, 0, 0, 0)}


single_col_lines = [fake_line(72.0)] * 20 + [fake_line(90.0)] * 5  # body + indents
check("detect_columns: ordinary indented single-column prose reads as 1 column",
      tp.detect_columns(single_col_lines, page_width=468.0) == 1)

two_col_lines = [fake_line(72.0)] * 15 + [fake_line(320.0)] * 15
check("detect_columns: two well-separated x0 clusters read as 2 columns",
      tp.detect_columns(two_col_lines, page_width=600.0) == 2)

check("detect_columns: empty input is 1 column",
      tp.detect_columns([], page_width=468.0) == 1)


print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
else:
    print("All checks passed.")
sys.exit(0 if not failures else 1)
