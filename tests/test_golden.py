#!/usr/bin/env python3
"""Golden-file regression test for the translation pipeline.

Runs a synthetic fixture (see fixtures.py) through process_pdf() with
load_model()/translate() stubbed out -- no ~2GB model load, no network, no
non-determinism from an actual LLM -- exercising the real, deterministic
extraction -> paragraph-splitting -> marker/italic marking -> reflow ->
page-fit -> redact/re-insert pipeline end to end. This is the harness Round
1's audit suggested and Round 3's audit built ad hoc to catch several bugs
that unit-level checks alone had missed (see AUDIT_FIXES.md).

Assertions are split across two different views of the pipeline, each
checking what it can actually prove:

- the *texts handed to translate()* -- captured via the stub -- prove what
  split_page_into_paragraphs()/join_paragraph_lines() decided while reading
  the SOURCE fixture (paragraph splitting, footnote-marker rewriting,
  dehyphenation). This is the only reliable way to check those decisions:
  re-running split_page_into_paragraphs() on the *rendered output* doesn't
  work, because by then footnote markers are just plain same-size inline
  text and the marker-based split signal (Finding 3) has nothing left to
  key off -- that's expected, not a regression, since real translated
  output never carries a small-font marker span either.
- the *rendered output's plain text/spans* prove what actually ends up
  visible in the PDF (italics, the literal asterisk, the folio's position).

Plain asserts, no test framework dependency (none is in requirements.txt).
Run directly:
    ./venv/bin/python3 tests/test_golden.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf as fitz  # noqa: E402

import translate_pdf as tp  # noqa: E402
from fixtures import build_fixture  # noqa: E402


def stub_load_model(model_name, seed=0):
    return object(), object()


def make_stub_translate(calls):
    def stub_translate(model, tokenizer, german_text, temp=0.0, report=None):
        calls.append(german_text)
        # Identity "translation" -- this test cares about structural
        # correctness (paragraph splitting, marker/italic round-tripping,
        # hyphenation, folio pinning), not translation quality, and a real
        # model load would make the test slow, environment-dependent, and
        # non-deterministic. Expect process_pdf's own echo-detection
        # heuristic (Finding 10, Round 4) to fire and report a "probable
        # no-op/echo" WARNING on every longer paragraph here -- that's it
        # correctly noticing the identity stub is, in fact, an echo; not a
        # bug in the stub or the heuristic.
        return german_text
    return stub_translate


def run():
    tp.load_model = stub_load_model
    translate_calls = []
    tp.translate = make_stub_translate(translate_calls)

    failures = []

    def check(name, cond, detail=None):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}")
        if not cond:
            print(f"       {detail!r}")
            failures.append(name)

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        fixture_path = d / "golden_in.pdf"
        out_path = d / "golden_out.pdf"
        fixture_info = build_fixture(fixture_path)

        tp.process_pdf(str(fixture_path), str(out_path), "stub-model")

        # --- what was fed to translate() (source-side extraction/splitting) ---

        check(
            "exactly 5 paragraphs were sent to translate() "
            "(heading, 2 body, 2 footnote entries -- the folio bypasses "
            "translation as a digit-only page number)",
            len(translate_calls) == 5, translate_calls,
        )

        check(
            'both footnote entries were sent as SEPARATE translate() calls, '
            'each keeping its own marker (Finding 3 -- consecutive '
            "footnote entries must not merge into one run-on paragraph)",
            len(translate_calls) >= 5
            and translate_calls[3].strip().startswith("[1]")
            and translate_calls[4].strip().startswith("[2]"),
            translate_calls,
        )

        check(
            'the hyphenated line break within footnote entry 1 was rejoined '
            'correctly ("Schulte-Sasse", not "Schulte- Sasse" or '
            '"SchulteSasse") before being sent to translate() '
            "(Finding 5 / join_paragraph_lines dehyphenation)",
            len(translate_calls) >= 4 and "Schulte-Sasse" in translate_calls[3],
            translate_calls[3] if len(translate_calls) >= 4 else None,
        )

        # --- what actually ended up in the rendered output ---

        doc = fitz.open(str(out_path))
        check("output has exactly one page", len(doc) == 1, len(doc))

        page = doc[0]
        # insert_htmlbox's justify mode renders inter-word spaces as U+00A0
        # (non-breaking space); normalize to an ordinary space for content
        # comparisons below, which only care about the visible text.
        plain_text = page.get_text().replace("\xa0", " ")
        blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]

        check('heading text survived intact ("Einleitung")',
              "Einleitung" in plain_text, plain_text)

        check(
            'literal asterisk survived as a literal character, not '
            'absorbed into the italic run ("(* 1903)") (Finding 4)',
            "(* 1903)" in plain_text, plain_text,
        )

        check('italic run text survived ("Minima Moralia")',
              "Minima Moralia" in plain_text, plain_text)

        italic_spans = [
            s for b in blocks for l in b["lines"] for s in l["spans"]
            if tp.span_is_italic(s) and s["text"].strip()
        ]
        check(
            '"Minima Moralia" is still actually rendered in italics, not '
            "just present as plain text",
            any("Minima" in s["text"] or "Moralia" in s["text"] for s in italic_spans),
            [s["text"] for s in italic_spans],
        )

        check('both footnote markers are visible in the output ("[1]" and "[2]")',
              "[1]" in plain_text and "[2]" in plain_text, plain_text)

        check('folio survived in the output ("5")',
              any(s["text"].strip() == "5" for b in blocks
                  for l in b["lines"] for s in l["spans"]))

        folio_span = next(
            (s for b in blocks for l in b["lines"] for s in l["spans"]
             if s["text"].strip() == "5"),
            None,
        )
        check(
            "folio stayed pinned near its authored position, not dragged "
            "by the body's reflow (Round 2 regression)",
            folio_span is not None
            and abs(folio_span["bbox"][1] - fixture_info["folio_y"]) < 5.0,
            folio_span["bbox"] if folio_span else None,
        )

        doc.close()

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
    else:
        print("All checks passed.")
    return not failures


def test_golden_file():
    """Synthetic pytest entry point -- see tests/test_layout.py's copy of
    this docstring for why (Round 5 Finding 13)."""
    assert run()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
