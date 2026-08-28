#!/usr/bin/env python3
"""Render a copy of a PDF with detected layout structure drawn on top.

    ./venv/bin/python debug_layout.py in.pdf overlay.pdf [--pages 1-5]

Colours:
    blue    text region (flow area), numbered in reading order
    green   table region
    red     obstacle (image / vector figure)
    grey    paragraph bbox as split_lines_into_paragraphs sees it
    orange  detected column gutter

This exists because tuning gutter thresholds and figure clustering by reading
paragraph counts on stderr is not workable. Look at the boxes.
"""
import argparse
import sys

import pymupdf as fitz

import layout as L
from translate_pdf import parse_page_range, split_lines_into_paragraphs

BLUE = (0.15, 0.35, 0.85)
GREEN = (0.10, 0.55, 0.25)
RED = (0.85, 0.15, 0.15)
GREY = (0.55, 0.55, 0.55)
ORANGE = (0.95, 0.55, 0.10)


def _label(page, rect, text, color):
    page.draw_rect(rect, color=color, width=1.0)
    page.insert_text(
        fitz.Point(rect.x0 + 2, max(rect.y0 + 8, 8)),
        text, fontsize=7, color=color,
    )


def write_overlay(in_path, out_path, page_range=None, use_tables=True,
                  table_strategy="lines", report=print):
    doc = fitz.open(in_path)
    try:
        indices = range(len(doc)) if page_range is None else page_range
        for pno in indices:
            page = doc[pno]
            plan = L.analyze_page(page, use_tables=use_tables,
                                  table_strategy=table_strategy)

            for ob in plan["obstacles"]:
                _label(page, ob["rect"], ob["kind"], RED)

            for gx0, gx1, gy0, gy1 in plan["gutters"]:
                page.draw_rect(fitz.Rect(gx0, gy0, gx1, gy1),
                               color=ORANGE, width=0.6, dashes="[2 2] 0")

            for i, region in enumerate(plan["regions"], start=1):
                color = GREEN if region["kind"] == "table" else BLUE
                _label(page, region["rect"], f"R{i}:{region['kind']}", color)
                if region["kind"] != "text":
                    continue
                for para in split_lines_into_paragraphs(region["lines"]):
                    page.draw_rect(para["bbox"], color=GREY, width=0.4)

            report(f"page {pno + 1}: {len(plan['regions'])} region(s), "
                   f"{len(plan['obstacles'])} obstacle(s), "
                   f"{len(plan['tables'])} table(s)")
        doc.save(out_path, garbage=4, deflate=True)
    finally:
        doc.close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--pages", default=None)
    ap.add_argument("--no-tables", action="store_true")
    ap.add_argument("--table-strategy", default="lines",
                    choices=["lines", "lines_strict", "text"])
    args = ap.parse_args(argv)

    doc = fitz.open(args.input)
    npages = len(doc)
    doc.close()
    page_range = parse_page_range(args.pages, npages,
                                  warn=lambda w: print(w, file=sys.stderr))
    write_overlay(args.input, args.output, page_range,
                  use_tables=not args.no_tables,
                  table_strategy=args.table_strategy)


if __name__ == "__main__":
    main()
