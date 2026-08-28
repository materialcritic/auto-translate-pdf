#!/usr/bin/env python3
"""Page-structure detection for the translation pipeline.

Splits a page into:

  regions    ordered flow areas. Reflow happens *within* a region, so a
             paragraph that grows pushes down only its own column, not the
             one beside it. A single-column page yields exactly one region,
             which makes this a no-op for the documents the pipeline was
             originally built for.

  obstacles  rectangles that text must never be drawn over or redacted
             through: raster images and clusters of vector drawing paths.
             Kept separate from regions because an obstacle interrupts the
             flow inside a region rather than partitioning the page.

  tables     regions of kind "table", translated cell by cell instead of
             paragraph by paragraph.

Column detection is deliberately not a generic recursive XY-cut. XY-cut cuts
at the widest whitespace valley in either direction, which on ordinary prose
splits at every section break and every wide paragraph gap -- harmless for
segmentation, but here a region boundary is also a *reflow* boundary, so
spurious cuts turn one flowing column into a stack of tiny fixed-height boxes
that clip as soon as English runs longer than German. Instead: find real
vertical gutters, then use full-width spanning lines (headings) as the only
horizontal cuts.
"""
import math

import pymupdf as fitz

# --- tunables -------------------------------------------------------------

MIN_GUTTER_FRAC = 0.035   # gutter width, as a fraction of the region width.
                          # An indented-paragraph first line sits 12-24pt off
                          # the margin; a real gutter is far wider. Keep this
                          # generous or ordinary prose reads as two columns.
MIN_GUTTER_PT = 10.0      # absolute floor for the above on narrow regions
SPAN_LINE_FRAC = 0.60     # a line wider than this fraction of the region is
                          # assumed to span columns; excluded from the x
                          # coverage histogram that finds gutters, or a single
                          # full-width heading would bridge every gutter and
                          # hide the column structure entirely
MIN_COL_FRAC = 0.12       # a "column" narrower than this is noise
MIN_COL_LINES = 3         # ...as is one with fewer lines than this
SPAN_BLOCK_GAP = 8.0      # pt; spanning lines closer together than this are
                          # one heading block, not two
MAX_COL_DEPTH = 2         # recursion guard for columns inside columns

OBSTACLE_MIN_AREA_FRAC = 0.004  # of page area; smaller drawings are bullets,
                                # logos in a running head, math glyphs
OBSTACLE_MAX_AREA_FRAC = 0.85   # larger than this is a page background or
                                # border box, not a figure -- blocking flow
                                # for it would empty the page
RULE_MAX_THICKNESS = 3.0        # pt; a thinner drawing is a horizontal rule
                                # or an underline, which text may sit next to
OBSTACLE_MERGE_PAD = 4.0        # pt; gap below which two drawing rects are
                                # treated as parts of one figure


# --- helpers --------------------------------------------------------------

def line_rect(line):
    return fitz.Rect(line["bbox"])


def _mid_y(line):
    b = line["bbox"]
    return (b[1] + b[3]) / 2.0


def page_text_lines(page, clip=None):
    """Every non-empty text line on the page, as PyMuPDF line dicts."""
    d = page.get_text("dict", clip=clip)
    return [
        l
        for b in d["blocks"] if b["type"] == 0
        for l in b["lines"] if l["spans"]
    ]


# --- obstacles ------------------------------------------------------------

def _merge_rects(rects, pad=OBSTACLE_MERGE_PAD):
    """Union rects that touch or nearly touch, repeatedly until stable.

    A vector chart arrives from get_drawings() as hundreds of separate path
    rects (each axis tick, each bar, each label rule). Treated individually
    every one is below OBSTACLE_MIN_AREA_FRAC and gets dropped, so the chart
    is invisible to the reflow and text lands on top of it. Merged first,
    the whole chart is one obstacle of the right size.
    """
    rects = [fitz.Rect(r) for r in rects]
    changed = True
    while changed:
        changed = False
        out = []
        for r in rects:
            for i, o in enumerate(out):
                grown = fitz.Rect(o)
                grown.x0 -= pad
                grown.y0 -= pad
                grown.x1 += pad
                grown.y1 += pad
                if grown.intersects(r):
                    out[i] = o | r
                    changed = True
                    break
            else:
                out.append(r)
        rects = out
    return rects


def find_obstacles(page, table_rects=()):
    """Rects that text must not be drawn over: raster images and clustered
    vector figures.

    Drawings inside a detected table bbox are dropped -- those are the
    table's own rules, and treating them as a figure would make the table's
    interior unwritable by the very code that needs to write into it.
    """
    page_area = abs(page.rect.get_area()) or 1.0
    table_rects = [fitz.Rect(t) for t in table_rects]

    image_rects = []
    for info in page.get_images(full=True):
        try:
            image_rects.extend(fitz.Rect(r) for r in page.get_image_rects(info[0]))
        except Exception:
            continue

    drawing_rects = []
    for d in page.get_drawings():
        r = fitz.Rect(d["rect"])
        if r.is_empty or r.is_infinite:
            continue
        if min(r.width, r.height) <= RULE_MAX_THICKNESS:
            continue  # a rule / underline / table border, not a figure
        if any(t.intersects(r) for t in table_rects):
            continue
        if d.get("fill") == (1.0, 1.0, 1.0) and d.get("color") in (None, (1.0, 1.0, 1.0)):
            # A solid white fill (with no stroke, or a white one -- PyMuPDF
            # reports both fill and stroke as white for a plain filled rect
            # with no visible border) is what
            # page.apply_redactions(fill=(1,1,1)) itself bakes into the
            # page as real vector content once applied -- on any *later*
            # pass over this pipeline's own output (a second
            # --reformat-only run, or the watcher re-triggering), that
            # white-out rectangle would otherwise be picked up here as a
            # "figure" the same size and position as the very paragraph it
            # covers, and then apply_text_redactions would skip redacting
            # that exact area to avoid "redacting through a figure" --
            # leaving the old text underneath un-erased while new text is
            # drawn on top of it. A real figure is essentially never a
            # borderless solid-white rectangle with no other content, so
            # this is a safe exclusion.
            continue
        drawing_rects.append(r)

    obstacles = []
    for r in _merge_rects(image_rects, pad=1.0):
        obstacles.append({"kind": "image", "rect": r})
    for r in _merge_rects(drawing_rects):
        obstacles.append({"kind": "drawing", "rect": r})

    keep = []
    for ob in obstacles:
        frac = abs(ob["rect"].get_area()) / page_area
        if frac < OBSTACLE_MIN_AREA_FRAC or frac > OBSTACLE_MAX_AREA_FRAC:
            continue
        keep.append(ob)
    keep.sort(key=lambda o: (o["rect"].y0, o["rect"].x0))
    return keep


# --- tables ---------------------------------------------------------------

def find_tables(page, strategy="lines"):
    """Detected tables as region dicts.

    `strategy="lines"` (the default) needs drawn borders and misses
    borderless tables entirely; `strategy="text"` catches those but
    false-positives on ordinary prose, including on footnote/reference runs,
    where it would turn a paragraph list into a one-column "table" and
    translate every entry as an isolated cell. Lines by default, text as an
    explicit opt-in.
    """
    try:
        finder = page.find_tables(strategy=strategy)
    except Exception:
        return []
    out = []
    for t in finder.tables:
        rect = fitz.Rect(t.bbox)
        if rect.is_empty or abs(rect.get_area()) < 400:
            continue
        if t.row_count < 2 or t.col_count < 2:
            continue  # a single row or column is a list, not a table
        out.append({"kind": "table", "rect": rect, "table": t, "lines": []})
    out.sort(key=lambda r: (r["rect"].y0, r["rect"].x0))
    return out


# --- columns --------------------------------------------------------------

def find_column_split(lines, area):
    """Column x-intervals inside `area`, or None if it's single-column.

    Works off a 1pt-resolution x coverage histogram of line bboxes, with
    obviously-spanning lines excluded (see SPAN_LINE_FRAC). Covered runs
    separated by a gap narrower than the gutter threshold are merged, so the
    two x0 populations ordinary indented prose produces (flush margin and
    indented first line) never read as two columns.
    """
    if not lines:
        return None
    body = [
        l for l in lines
        if (l["bbox"][2] - l["bbox"][0]) < SPAN_LINE_FRAC * area.width
    ]
    if len(body) < 2 * MIN_COL_LINES:
        return None

    x_lo = int(math.floor(area.x0))
    x_hi = int(math.ceil(area.x1))
    width = x_hi - x_lo
    if width <= 0:
        return None

    cov = bytearray(width)
    for l in body:
        a = max(0, int(math.floor(l["bbox"][0])) - x_lo)
        b = min(width, int(math.ceil(l["bbox"][2])) - x_lo)
        for i in range(a, b):
            cov[i] = 1

    runs = []
    i = 0
    while i < width:
        if cov[i]:
            j = i
            while j < width and cov[j]:
                j += 1
            runs.append([x_lo + i, x_lo + j])
            i = j
        else:
            i += 1
    if not runs:
        return None

    min_gutter = max(MIN_GUTTER_PT, MIN_GUTTER_FRAC * area.width)
    merged = [runs[0]]
    for r in runs[1:]:
        if r[0] - merged[-1][1] < min_gutter:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    if len(merged) < 2:
        return None

    cols = []
    for lo, hi in merged:
        if (hi - lo) < MIN_COL_FRAC * area.width:
            continue
        n = sum(1 for l in body if lo - 1 <= _mid_x(l) <= hi + 1)
        if n < MIN_COL_LINES:
            continue
        cols.append((float(lo), float(hi)))
    return cols if len(cols) >= 2 else None


def _mid_x(line):
    b = line["bbox"]
    return (b[0] + b[2]) / 2.0


def gutters_from_columns(cols, area):
    """The whitespace bands between columns, for the debug overlay."""
    return [
        (cols[i][1], cols[i + 1][0], area.y0, area.y1)
        for i in range(len(cols) - 1)
    ]


def _column_of(line, cols):
    """Index of the column containing this line's midpoint, or None."""
    mx = _mid_x(line)
    for i, (lo, hi) in enumerate(cols):
        if lo - 1 <= mx <= hi + 1:
            return i
    return None


def _spans_columns(line, cols):
    """True if the line's bbox meaningfully overlaps more than one column."""
    x0, x1 = line["bbox"][0], line["bbox"][2]
    touched = 0
    for lo, hi in cols:
        overlap = min(x1, hi) - max(x0, lo)
        if overlap > 0.25 * (hi - lo):
            touched += 1
    return touched > 1


def _group_spanning(lines):
    """Consecutive spanning lines close in y are one heading block."""
    lines = sorted(lines, key=lambda l: l["bbox"][1])
    blocks = []
    for l in lines:
        if blocks and l["bbox"][1] - blocks[-1][-1]["bbox"][3] <= SPAN_BLOCK_GAP:
            blocks[-1].append(l)
        else:
            blocks.append([l])
    return blocks


def _leaf(area, lines, kind="text"):
    return {"kind": kind, "rect": fitz.Rect(area), "lines": list(lines)}


def _split_band_by_columns(lines, cols, band, depth):
    out = []
    for i, (lo, hi) in enumerate(cols):
        col_lines = [l for l in lines if _column_of(l, cols) == i]
        if not col_lines:
            continue
        rect = fitz.Rect(lo, band.y0, hi, band.y1)
        out.extend(build_text_regions(col_lines, rect, depth + 1))
    return out


def build_text_regions(lines, area, depth=0):
    """Ordered flow regions for `lines` inside `area`.

    Returned in reading order: a spanning heading block, then that band's
    columns left to right, then the next spanning block, and so on. Ordering
    here is what stops a two-column page from being zipped row by row.
    """
    lines = [l for l in lines if l["spans"]]
    if not lines:
        return []
    if depth >= MAX_COL_DEPTH:
        return [_leaf(area, lines)]

    cols = find_column_split(lines, area)
    if not cols:
        return [_leaf(area, lines)]

    spanning = [l for l in lines if _spans_columns(l, cols)]
    body = [l for l in lines if l not in spanning]
    if not spanning:
        return _split_band_by_columns(body, cols, area, depth)

    regions = []
    cursor = area.y0
    for block in _group_spanning(spanning):
        by0 = min(l["bbox"][1] for l in block)
        by1 = max(l["bbox"][3] for l in block)
        band_lines = [l for l in body if cursor <= _mid_y(l) < by0]
        if band_lines:
            band = fitz.Rect(area.x0, cursor, area.x1, by0)
            regions.extend(_split_band_by_columns(band_lines, cols, band, depth))
        regions.append(
            _leaf(fitz.Rect(area.x0, by0, area.x1, by1), block)
        )
        cursor = by1
    tail = [l for l in body if _mid_y(l) >= cursor]
    if tail:
        band = fitz.Rect(area.x0, cursor, area.x1, area.y1)
        regions.extend(_split_band_by_columns(tail, cols, band, depth))
    return regions


# --- top level ------------------------------------------------------------

def analyze_page(page, use_tables=True, table_strategy="lines"):
    """{"regions": [...], "obstacles": [...], "tables": [...],
        "gutters": [...], "columns": int}

    `regions` is in reading order and includes table regions, inserted at
    their y position. A single-column page with no figures returns exactly
    one text region covering the page -- i.e. the pre-existing behaviour.
    """
    tables = find_tables(page, table_strategy) if use_tables else []
    table_rects = [t["rect"] for t in tables]
    obstacles = find_obstacles(page, table_rects)

    lines = page_text_lines(page)
    # Lines inside a table belong to the table, not to prose flow; leaving
    # them in the general pool translates every cell twice and lets the
    # prose splitter zip the rows together as run-on paragraphs.
    free_lines = [
        l for l in lines
        if not any(t.intersects(line_rect(l)) for t in table_rects)
    ]

    if free_lines:
        x0 = min(l["bbox"][0] for l in free_lines)
        x1 = max(l["bbox"][2] for l in free_lines)
        y0 = min(l["bbox"][1] for l in free_lines)
        y1 = max(l["bbox"][3] for l in free_lines)
        area = fitz.Rect(x0, y0, x1, y1)
    else:
        area = fitz.Rect(page.rect)

    regions = build_text_regions(free_lines, area)

    cols = find_column_split(free_lines, area) or []
    gutters = gutters_from_columns(cols, area) if len(cols) >= 2 else []

    for t in tables:
        idx = len(regions)
        for i, r in enumerate(regions):
            if r["rect"].y0 > t["rect"].y0:
                idx = i
                break
        regions.insert(idx, t)

    return {
        "regions": regions,
        "obstacles": obstacles,
        "tables": tables,
        "gutters": gutters,
        "columns": max(1, len(cols)),
    }
