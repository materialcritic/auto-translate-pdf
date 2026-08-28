#!/usr/bin/env python3
"""Phase 1 acceptance tests for layout.py (LAYOUT_SUPPORT.md section 3).
Named test_regions.py, not test_layout.py, to avoid colliding with the
existing tests/test_layout.py (Round 4/5 layout-correctness regressions,
unrelated to this module).

Plain asserts, no test framework dependency, matching the existing style.
Run directly:
    ./venv/bin/python3 tests/test_regions.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf as fitz  # noqa: E402

import layout as L  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus"
failures = []


def check(name, cond, detail=None):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        print(f"       {detail!r}")
        failures.append(name)


def _plan(name, pno=0):
    doc = fitz.open(str(CORPUS / name))
    try:
        return L.analyze_page(doc[pno]), doc[pno].rect
    finally:
        doc.close()


plan, _ = _plan("single_column.pdf")
check("single_column.pdf: exactly 1 column",
      plan["columns"] == 1, plan["columns"])
check("single_column.pdf: exactly 1 text region",
      len([r for r in plan["regions"] if r["kind"] == "text"]) == 1,
      [r["kind"] for r in plan["regions"]])

plan, _ = _plan("two_column.pdf")
check("two_column.pdf: 2 columns detected",
      plan["columns"] == 2, plan["columns"])
check("two_column.pdf: at least 2 regions",
      len(plan["regions"]) >= 2, len(plan["regions"]))

plan, rect = _plan("two_column_heading.pdf")
check("two_column_heading.pdf: spanning heading is its own region, first, "
      "and wide",
      bool(plan["regions"]) and plan["regions"][0]["rect"].width > 0.6 * rect.width,
      [r["rect"] for r in plan["regions"][:1]])
check("two_column_heading.pdf: exactly 3 regions (heading, then 2 columns "
      "below it) -- not the heading alone bridging the gutter and hiding "
      "the column structure",
      len(plan["regions"]) == 3, [r["rect"] for r in plan["regions"]])

plan, _ = _plan("footnotes.pdf")
check("footnotes.pdf: the regression that matters -- documents this "
      "pipeline already handles must keep reading as single-column",
      plan["columns"] == 1, plan["columns"])

plan, _ = _plan("figure_vector.pdf")
check("figure_vector.pdf: the whole chart is ONE merged obstacle, not "
      "dozens of individually-too-small drawing rects",
      len(plan["obstacles"]) == 1, plan["obstacles"])

plan, _ = _plan("figure_inline.pdf")
check("figure_inline.pdf: the image is found as an obstacle",
      len(plan["obstacles"]) == 1 and plan["obstacles"][0]["kind"] == "image",
      plan["obstacles"])

plan, _ = _plan("table_ruled.pdf")
check("table_ruled.pdf: exactly 1 table found",
      len(plan["tables"]) == 1, plan["tables"])
check("table_ruled.pdf: the table's own ruling lines are not also reported "
      "as a separate drawing obstacle",
      not any(ob["kind"] == "drawing" for ob in plan["obstacles"]),
      plan["obstacles"])


print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
else:
    print("All checks passed.")


def test_all_checks():
    """Synthetic pytest entry point -- see tests/test_layout.py's copy of
    this docstring for why."""
    assert not failures, f"{len(failures)} check(s) failed: {', '.join(failures)}"


if __name__ == "__main__":
    sys.exit(0 if not failures else 1)
