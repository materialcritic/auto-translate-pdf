# auto-translate-pdf — logging & last-run record

_Generated 2026-08-29. Covers the watcher copy actually running on this machine:_
_`~/Scripts/auto_translate_pdf.py` (an older build that keeps its files **inside**_
_the watched folder — the repo version at `Claude_Cowork/auto-translate-pdf/scripts/`_
_moved most of them to `~/Library/Application Support/auto_translate_pdf/`)._

---

## How logs are stored

The watcher (`~/Scripts/auto_translate_pdf.py`) writes three files, plus a lock:

| File | Path | Purpose | Lifetime |
|---|---|---|---|
| **Result log** | `~/Translate/translate_log.csv` | One row per finished file: `timestamp,input,output,status`. `status` is `ok` or `error`. Appended via `log_result()`. | Permanent, append-only. The durable record. |
| **Progress log** | `~/Translate/.translate_progress.log` | Verbose per-page / per-paragraph trace written by `report()` — every paragraph's char-count in→out, every `region content rescaled` event, `WARNING:` / `OVERFLOW:` lines, `skipped near-empty paragraph`, the translated document title, and `start:` / `done:` bookends. | Grows across runs; this old copy does **not** rotate it. (The repo version truncates past 5 MB.) |
| **State** | `~/Translate/.auto_translate_state.json` | Not a log — bookkeeping. `done` = keys the watcher won't reprocess, `attempts` = per-file retry counter, `failed` = permanently given-up files (after `MAX_ATTEMPTS = 3`). Keys are the path relative to `~/Translate`; newer entries also carry a `name:size:mtime` form. | Permanent. |
| Lock | `~/Library/Application Support/auto_translate_pdf.lock` | `flock` held for the duration of a run so concurrent Folder Action triggers queue instead of racing / double-loading the model. | Recreated each run. |

Notes:

- **Timestamps** in `.translate_progress.log` are `[HH:MM:SS]` only — no date. Use the
  `translate_log.csv` row (full timestamp) to anchor a run to a day.
- A `start:` line has no matching `done:` if the run crashed or is still going.
- `.DS_Store` and the `.` prefix on the two dotfiles keep them from cluttering Finder;
  the dot does **not** stop this old copy's own bookkeeping writes from re-triggering
  the Folder Action (a reason the repo version relocated them out of the folder).
- Nothing logs the per-paragraph German/English *text* — only lengths. The only text
  echoed is the offending paragraph's leading ~60 chars on a `WARNING: ... truncation`
  line, and the document title.

### Reading them

```bash
cat ~/Translate/translate_log.csv                       # result history
tail -f ~/Translate/.translate_progress.log             # live progress
cat ~/Translate/.auto_translate_state.json              # what's done/failed
grep -E 'WARNING|OVERFLOW|^\[.*\] (start|done):' ~/Translate/.translate_progress.log
```

---

## Last translated file

**`agnoli_der-staat-des-kapitals-8cf1996ef9_ocr.pdf`** → **`agnoli_der-staat-des-kapitals-8cf1996ef9_ocr_en.pdf`**

| | |
|---|---|
| Source size | 4,544,673 bytes |
| Output size | 5,386,190 bytes |
| Pages | 35 (all 2-column) |
| Started | 2026-08-29 02:34:30 |
| Finished | 2026-08-29 03:17:38 (~43 min) |
| Status | `ok` |
| Document title | `'Untitled'` → `Untitled` |

### `translate_log.csv` (full file)

```csv
timestamp,input,output,status
2026-08-27 02:28:05,"1-95-43-56.pdf","1-95-43-56_en.pdf","ok"
2026-08-29 03:17:38,"agnoli_der-staat-des-kapitals-8cf1996ef9_ocr.pdf","agnoli_der-staat-des-kapitals-8cf1996ef9_ocr_en.pdf","ok"
```

### `.auto_translate_state.json` (full file)

```json
{
  "done": [
    "1-95-43-56.pdf",
    "1-95-43-56.pdf:43038:1787731189",
    "agnoli_der-staat-des-kapitals-8cf1996ef9_ocr.pdf",
    "zum-verh-ltnis-von-literatur-und-ffentlichkeit-bis-zum-deutschen-vorm-rz-oder-wi--stein2-0e99d33e.pdf"
  ],
  "attempts": {},
  "failed": {}
}
```

### Notable events during this run

Pulled from `.translate_progress.log` (the full per-paragraph trace for this run is below):

- **2 truncation warnings** — model output much shorter than source:
  - `[02:34:50] page 1` — paragraph `'*u* *oder burgerliche Gesellschaft?* *"Zivilgesellschaft*'`
  - `[03:12:06] page 33` — paragraph `'schaftlicher Sachverhalt, kein Naturgesetz. Sie kann slch Je'` (293 → 22 chars)
- **7 hard `OVERFLOW` events** — region still overflowed at the 0.72× minimum font scale, so some text on that region may be clipped:
  - `[02:35:48]` by 77pt · `[02:41:06]` by 344pt · `[02:44:09]` by 7pt · `[02:44:25]` by 27pt · `[02:44:46]` by 278pt · `[02:44:52]` by 1pt · `[03:12:45]` by 5pt · `[03:13:08]` by 6pt · `[03:16:25]` by 321pt (page 34)
- **4 `measure_height` disagreement warnings** — paragraph rendered at 0.68× / 0.99× / 0.94× / 0.85× vs. the predicted size (`[02:36:34]`, `[02:48:13]`, `[02:49:34]`, `[02:57:14]`).
- Many `region content rescaled to N×` lines — normal two-stage fit shrinking gaps/type to fit a column; only the `OVERFLOW` ones above are failures.
- Several `skipped near-empty paragraph [1–3 chars]` — expected (stray marker / dash guard).

The big overflows (278–344pt on pages ~7, ~14, 34) are worth eyeballing in the
output PDF — that much excess at minimum scale means real clipped text on those
pages. Likely OCR-noise paragraphs or mis-detected column regions in the scan.

---

## Full progress-log trace for this run

`.translate_progress.log` lines 542–1195 (`[HH:MM:SS] start:` → `done:`):

```text
[02:34:30] start: agnoli_der-staat-des-kapitals-8cf1996ef9_ocr.pdf
[02:34:42] --- page 1 (1/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:34:46]   page 1: paragraph translated [110 chars] -> [117 chars]
[02:34:48]   page 1: paragraph translated [22 chars] -> [20 chars]
[02:34:50]   page 1: WARNING: output is much shorter than the source (probable truncation) (paragraph: '*u* *oder burgerliche Gesellschaft?* *"Zivilgesellschaft*')
[02:34:50]   page 1: paragraph translated [57 chars] -> [15 chars]
[02:34:52]   page 1: paragraph translated [54 chars] -> [70 chars]
[02:34:56]   page 1: paragraph translated [226 chars] -> [187 chars]
[02:35:10]   page 1: paragraph translated [1594 chars] -> [1333 chars]
[02:35:10]   page 1: skipped near-empty paragraph [2 chars] -> skipped (too little content)
[02:35:11] --- page 2 (2/35) --- 19 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:35:14]   page 2: paragraph translated [110 chars] -> [121 chars]
[02:35:14]   region content rescaled to 0.92x to fit
[02:35:16]   page 2: paragraph translated [40 chars] -> [39 chars]
[02:35:16]   region content rescaled to 0.88x to fit
[02:35:18]   page 2: paragraph translated [11 chars] -> [11 chars]
[02:35:18]   region content rescaled to 0.88x to fit
[02:35:22]   page 2: paragraph translated [193 chars] -> [159 chars]
[02:35:22]   region content rescaled to 0.92x to fit
[02:35:24]   page 2: paragraph translated [37 chars] -> [32 chars]
[02:35:24]   region content rescaled to 0.88x to fit
[02:35:28]   page 2: paragraph translated [242 chars] -> [246 chars]
[02:35:28]   region content rescaled to 0.96x to fit
[02:35:30]   page 2: paragraph translated [21 chars] -> [19 chars]
[02:35:30]   region content rescaled to 0.88x to fit
[02:35:32]   page 2: paragraph translated [59 chars] -> [58 chars]
[02:35:35]   page 2: paragraph translated [61 chars] -> [51 chars]
[02:35:38]   page 2: paragraph translated [164 chars] -> [146 chars]
[02:35:41]   page 2: paragraph translated [167 chars] -> [185 chars]
[02:35:42]   region content rescaled to 0.88x to fit
[02:35:44]   page 2: paragraph translated [32 chars] -> [36 chars]
[02:35:44]   region content rescaled to 0.88x to fit
[02:35:48]   page 2: paragraph translated [16 chars] -> [213 chars]
[02:35:48]   OVERFLOW: region content overflows by 77pt even at 0.72x font scale -- some text may be clipped
[02:35:52]   page 2: paragraph translated [338 chars] -> [330 chars]
[02:35:52]   region content rescaled to 0.96x to fit
[02:35:55]   page 2: paragraph translated [41 chars] -> [47 chars]
[02:35:55]   region content rescaled to 0.84x to fit
[02:35:57]   page 2: paragraph translated [9 chars] -> [10 chars]
[02:36:00]   page 2: paragraph translated [234 chars] -> [211 chars]
[02:36:00]   region content rescaled to 0.96x to fit
[02:36:03]   page 2: paragraph translated [34 chars] -> [36 chars]
[02:36:03]   region content rescaled to 0.88x to fit
[02:36:05]   page 2: paragraph translated [7 chars] -> [7 chars]
[02:36:05]   region content rescaled to 0.88x to fit
[02:36:09]   page 2: paragraph translated [361 chars] -> [314 chars]
[02:36:13]   page 2: paragraph translated [217 chars] -> [218 chars]
[02:36:26]   page 2: paragraph translated [1406 chars] -> [1122 chars]
[02:36:33]   page 2: paragraph translated [741 chars] -> [629 chars]
[02:36:34]   WARNING: paragraph rendered at 0.68x -- measure_height disagreed with the actual render
[02:36:35] --- page 3 (3/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:36:41]   page 3: paragraph translated [425 chars] -> [461 chars]
[02:36:56]   page 3: paragraph translated [1620 chars] -> [1518 chars]
[02:36:57]   page 3: skipped near-empty paragraph [1 chars] -> skipped (too little content)
[02:37:03]   page 3: paragraph translated [467 chars] -> [416 chars]
[02:37:18]   page 3: paragraph translated [1490 chars] -> [1298 chars]
[02:37:23]   page 3: paragraph translated [364 chars] -> [304 chars]
[02:37:24] --- page 4 (4/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:37:27]   page 4: paragraph translated [95 chars] -> [124 chars]
[02:37:36]   page 4: paragraph translated [823 chars] -> [629 chars]
[02:37:46]   page 4: paragraph translated [1086 chars] -> [944 chars]
[02:37:49]   page 4: paragraph translated [64 chars] -> [58 chars]
[02:37:51]   page 4: paragraph translated [61 chars] -> [74 chars]
[02:37:54]   page 4: paragraph translated [57 chars] -> [61 chars]
[02:37:56]   page 4: paragraph translated [61 chars] -> [69 chars]
[02:37:58]   page 4: paragraph translated [58 chars] -> [40 chars]
[02:38:01]   page 4: paragraph translated [61 chars] -> [58 chars]
[02:38:03]   page 4: paragraph translated [53 chars] -> [63 chars]
[02:38:05]   page 4: paragraph translated [60 chars] -> [51 chars]
[02:38:08]   page 4: paragraph translated [65 chars] -> [99 chars]
[02:38:10]   page 4: paragraph translated [65 chars] -> [74 chars]
[02:38:13]   page 4: paragraph translated [63 chars] -> [89 chars]
[02:38:16]   page 4: paragraph translated [125 chars] -> [125 chars]
[02:38:30]   page 4: paragraph translated [1503 chars] -> [1375 chars]
[02:38:33]   page 4: paragraph translated [125 chars] -> [114 chars]
[02:38:34]   region content rescaled to 0.96x to fit
[02:38:35] --- page 5 (5/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:38:43]   page 5: paragraph translated [784 chars] -> [683 chars]
[02:38:46]   page 5: paragraph translated [55 chars] -> [69 chars]
[02:38:52]   page 5: paragraph translated [478 chars] -> [485 chars]
[02:38:55]   page 5: paragraph translated [196 chars] -> [170 chars]
[02:39:02]   page 5: paragraph translated [585 chars] -> [505 chars]
[02:39:10]   page 5: paragraph translated [797 chars] -> [674 chars]
[02:39:18]   page 5: paragraph translated [621 chars] -> [542 chars]
[02:39:22]   page 5: paragraph translated [211 chars] -> [188 chars]
[02:39:29]   page 5: paragraph translated [676 chars] -> [600 chars]
[02:39:31] --- page 6 (6/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:39:41]   page 6: paragraph translated [1137 chars] -> [986 chars]
[02:39:46]   page 6: paragraph translated [303 chars] -> [268 chars]
[02:39:51]   page 6: paragraph translated [360 chars] -> [337 chars]
[02:39:57]   page 6: paragraph translated [491 chars] -> [351 chars]
[02:39:59]   page 6: paragraph translated [5 chars] -> [5 chars]
[02:40:01]   page 6: paragraph translated [53 chars] -> [53 chars]
[02:40:05]   page 6: paragraph translated [13 chars] -> [251 chars]
[02:40:07]   page 6: paragraph translated [46 chars] -> [78 chars]
[02:40:10]   page 6: paragraph translated [44 chars] -> [42 chars]
[02:40:13]   page 6: paragraph translated [47 chars] -> [60 chars]
[02:40:17]   page 6: paragraph translated [107 chars] -> [110 chars]
[02:40:24]   page 6: paragraph translated [228 chars] -> [253 chars]
[02:40:26]   page 6: paragraph translated [56 chars] -> [87 chars]
[02:40:30]   page 6: paragraph translated [148 chars] -> [171 chars]
[02:40:33]   page 6: paragraph translated [58 chars] -> [70 chars]
[02:40:36]   page 6: paragraph translated [79 chars] -> [87 chars]
[02:40:41]   page 6: paragraph translated [279 chars] -> [257 chars]
[02:40:45]   page 6: paragraph translated [156 chars] -> [171 chars]
[02:40:48]   page 6: paragraph translated [55 chars] -> [60 chars]
[02:40:50]   page 6: paragraph translated [102 chars] -> [98 chars]
[02:40:52]   page 6: paragraph translated [40 chars] -> [40 chars]
[02:40:57]   page 6: paragraph translated [255 chars] -> [270 chars]
[02:41:02]   page 6: paragraph translated [220 chars] -> [175 chars]
[02:41:06]   OVERFLOW: region content overflows by 344pt even at 0.72x font scale -- some text may be clipped
[02:41:08] --- page 7 (7/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:41:13]   page 7: paragraph translated [383 chars] -> [363 chars]
[02:41:17]   page 7: paragraph translated [105 chars] -> [125 chars]
[02:41:26]   page 7: paragraph translated [833 chars] -> [752 chars]
[02:41:32]   page 7: paragraph translated [342 chars] -> [359 chars]
[02:41:37]   page 7: paragraph translated [279 chars] -> [286 chars]
[02:41:40]   page 7: paragraph translated [63 chars] -> [55 chars]
[02:41:43]   page 7: paragraph translated [63 chars] -> [33 chars]
[02:41:45]   page 7: paragraph translated [61 chars] -> [54 chars]
[02:41:47]   page 7: paragraph translated [62 chars] -> [38 chars]
[02:41:50]   page 7: paragraph translated [62 chars] -> [62 chars]
[02:41:52]   page 7: paragraph translated [61 chars] -> [80 chars]
[02:41:55]   page 7: paragraph translated [58 chars] -> [67 chars]
[02:41:58]   page 7: paragraph translated [62 chars] -> [65 chars]
[02:42:00]   page 7: paragraph translated [60 chars] -> [58 chars]
[02:42:03]   page 7: paragraph translated [56 chars] -> [55 chars]
[02:42:06]   page 7: paragraph translated [59 chars] -> [69 chars]
[02:42:08]   page 7: paragraph translated [62 chars] -> [64 chars]
[02:42:10]   page 7: paragraph translated [36 chars] -> [42 chars]
[02:42:12]   page 7: paragraph translated [59 chars] -> [52 chars]
[02:42:14]   page 7: paragraph translated [63 chars] -> [58 chars]
[02:42:17]   page 7: paragraph translated [61 chars] -> [65 chars]
[02:42:19]   page 7: paragraph translated [61 chars] -> [73 chars]
[02:42:21]   page 7: paragraph translated [59 chars] -> [69 chars]
[02:42:24]   page 7: paragraph translated [64 chars] -> [63 chars]
[02:42:26]   page 7: paragraph translated [100 chars] -> [94 chars]
[02:42:37]   page 7: paragraph translated [1020 chars] -> [729 chars]
[02:42:40]   page 7: paragraph translated [59 chars] -> [55 chars]
[02:42:40]   region content rescaled to 0.96x to fit
[02:42:42] --- page 8 (8/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:42:47]   page 8: paragraph translated [246 chars] -> [227 chars]
[02:42:56]   page 8: paragraph translated [615 chars] -> [533 chars]
[02:43:00]   page 8: paragraph translated [185 chars] -> [166 chars]
[02:43:05]   page 8: paragraph translated [242 chars] -> [264 chars]
[02:43:18]   page 8: paragraph translated [1033 chars] -> [821 chars]
[02:43:29]   page 8: paragraph translated [732 chars] -> [659 chars]
[02:43:35]   page 8: paragraph translated [360 chars] -> [364 chars]
[02:43:53]   page 8: paragraph translated [1288 chars] -> [1101 chars]
[02:43:54] --- page 9 (9/35) --- 20 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:44:04]   page 9: paragraph translated [644 chars] -> [609 chars]
[02:44:07]   page 9: paragraph translated [37 chars] -> [38 chars]
[02:44:09]   OVERFLOW: region content overflows by 7pt even at 0.72x font scale -- some text may be clipped
[02:44:11]   page 9: paragraph translated [8 chars] -> [4 chars]
[02:44:11]   region content rescaled to 0.88x to fit
[02:44:15]   page 9: paragraph translated [126 chars] -> [164 chars]
[02:44:15]   region content rescaled to 0.80x to fit
[02:44:18]   page 9: paragraph translated [15 chars] -> [10 chars]
[02:44:18]   region content rescaled to 0.92x to fit
[02:44:21]   page 9: paragraph translated [179 chars] -> [150 chars]
[02:44:21]   region content rescaled to 0.92x to fit
[02:44:25]   page 9: paragraph translated [43 chars] -> [119 chars]
[02:44:25]   OVERFLOW: region content overflows by 27pt even at 0.72x font scale -- some text may be clipped
[02:44:25]   page 9: skipped near-empty paragraph [3 chars] -> skipped (too little content)
[02:44:25]   region content rescaled to 0.92x to fit
[02:44:28]   page 9: paragraph translated [111 chars] -> [118 chars]
[02:44:31]   page 9: paragraph translated [110 chars] -> [116 chars]
[02:44:31]   region content rescaled to 0.92x to fit
[02:44:34]   page 9: paragraph translated [83 chars] -> [77 chars]
[02:44:46]   page 9: paragraph translated [19 chars] -> [740 chars]
[02:44:46]   OVERFLOW: region content overflows by 278pt even at 0.72x font scale -- some text may be clipped
[02:44:50]   page 9: paragraph translated [115 chars] -> [121 chars]
[02:44:50]   region content rescaled to 0.92x to fit
[02:44:52]   page 9: paragraph translated [23 chars] -> [23 chars]
[02:44:52]   region content rescaled to 0.84x to fit
[02:44:52]   page 9: skipped near-empty paragraph [3 chars] -> skipped (too little content)
[02:44:52]   OVERFLOW: region content overflows by 1pt even at 0.72x font scale -- some text may be clipped
[02:44:58]   page 9: paragraph translated [430 chars] -> [350 chars]
[02:45:01]   page 9: paragraph translated [39 chars] -> [29 chars]
[02:45:01]   region content rescaled to 0.84x to fit
[02:45:03]   page 9: paragraph translated [16 chars] -> [14 chars]
[02:45:03]   region content rescaled to 0.88x to fit
[02:45:06]   page 9: paragraph translated [118 chars] -> [138 chars]
[02:45:07]   region content rescaled to 0.88x to fit
[02:45:09]   page 9: paragraph translated [42 chars] -> [48 chars]
[02:45:09]   region content rescaled to 0.96x to fit
[02:45:17]   page 9: paragraph translated [757 chars] -> [538 chars]
[02:45:21]   page 9: paragraph translated [352 chars] -> [340 chars]
[02:45:23]   page 9: paragraph translated [29 chars] -> [29 chars]
[02:45:26]   page 9: paragraph translated [27 chars] -> [52 chars]
[02:45:29]   page 9: paragraph translated [35 chars] -> [130 chars]
[02:45:33]   page 9: paragraph translated [208 chars] -> [201 chars]
[02:45:36]   page 9: paragraph translated [58 chars] -> [55 chars]
[02:45:38]   page 9: paragraph translated [25 chars] -> [20 chars]
[02:45:40]   page 9: paragraph translated [32 chars] -> [15 chars]
[02:45:42]   page 9: paragraph translated [4 chars] -> [4 chars]
[02:45:45]   page 9: paragraph translated [29 chars] -> [40 chars]
[02:45:48]   page 9: paragraph translated [94 chars] -> [144 chars]
[02:45:52]   page 9: paragraph translated [315 chars] -> [285 chars]
[02:45:55] --- page 10 (10/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:45:59]   page 10: paragraph translated [237 chars] -> [239 chars]
[02:46:12]   page 10: paragraph translated [1151 chars] -> [1036 chars]
[02:46:21]   page 10: paragraph translated [745 chars] -> [606 chars]
[02:46:24]   page 10: paragraph translated [100 chars] -> [84 chars]
[02:46:36]   page 10: paragraph translated [901 chars] -> [910 chars]
[02:46:53]   page 10: paragraph translated [1322 chars] -> [1134 chars]
[02:46:53]   page 10: skipped near-empty paragraph [2 chars] -> skipped (too little content)
[02:46:55]   page 10: paragraph translated [35 chars] -> [29 chars]
[02:46:59]   page 10: paragraph translated [12 chars] -> [201 chars]
[02:47:07]   page 10: paragraph translated [16 chars] -> [482 chars]
[02:47:10]   page 10: paragraph translated [34 chars] -> [25 chars]
[02:47:11]   region content rescaled to 0.84x to fit
[02:47:12] --- page 11 (11/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:47:14]   page 11: paragraph translated [37 chars] -> [33 chars]
[02:47:16]   page 11: paragraph translated [34 chars] -> [29 chars]
[02:47:26]   page 11: paragraph translated [754 chars] -> [671 chars]
[02:47:32]   page 11: paragraph translated [226 chars] -> [223 chars]
[02:47:45]   page 11: paragraph translated [1034 chars] -> [967 chars]
[02:47:48]   page 11: paragraph translated [108 chars] -> [112 chars]
[02:48:06]   page 11: paragraph translated [1690 chars] -> [1433 chars]
[02:48:08]   page 11: paragraph translated [56 chars] -> [67 chars]
[02:48:13]   page 11: paragraph translated [339 chars] -> [310 chars]
[02:48:13]   WARNING: paragraph rendered at 0.99x -- measure_height disagreed with the actual render
[02:48:14] --- page 12 (12/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:48:18]   page 12: paragraph translated [173 chars] -> [242 chars]
[02:48:21]   page 12: paragraph translated [243 chars] -> [219 chars]
[02:48:26]   page 12: paragraph translated [418 chars] -> [354 chars]
[02:48:38]   page 12: paragraph translated [1195 chars] -> [1122 chars]
[02:48:40]   page 12: paragraph translated [36 chars] -> [32 chars]
[02:48:43]   page 12: paragraph translated [55 chars] -> [74 chars]
[02:48:45]   page 12: paragraph translated [62 chars] -> [91 chars]
[02:48:48]   page 12: paragraph translated [59 chars] -> [81 chars]
[02:48:50]   page 12: paragraph translated [62 chars] -> [71 chars]
[02:48:51]   region content rescaled to 0.96x to fit
[02:48:54]   page 12: paragraph translated [221 chars] -> [200 chars]
[02:48:56]   page 12: paragraph translated [58 chars] -> [72 chars]
[02:48:59]   page 12: paragraph translated [42 chars] -> [38 chars]
[02:49:03]   page 12: paragraph translated [120 chars] -> [198 chars]
[02:49:06]   page 12: paragraph translated [165 chars] -> [150 chars]
[02:49:11]   page 12: paragraph translated [288 chars] -> [278 chars]
[02:49:15]   page 12: paragraph translated [219 chars] -> [233 chars]
[02:49:18]   page 12: paragraph translated [166 chars] -> [160 chars]
[02:49:24]   page 12: paragraph translated [459 chars] -> [433 chars]
[02:49:27]   page 12: paragraph translated [85 chars] -> [116 chars]
[02:49:30]   page 12: paragraph translated [238 chars] -> [216 chars]
[02:49:33]   page 12: paragraph translated [124 chars] -> [118 chars]
[02:49:34]   region content rescaled to 0.92x to fit
[02:49:34]   WARNING: paragraph rendered at 0.94x -- measure_height disagreed with the actual render
[02:49:36] --- page 13 (13/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:49:41]   page 13: paragraph translated [450 chars] -> [399 chars]
[02:49:48]   page 13: paragraph translated [700 chars] -> [610 chars]
[02:49:58]   page 13: paragraph translated [1006 chars] -> [886 chars]
[02:50:00]   page 13: paragraph translated [47 chars] -> [56 chars]
[02:50:03]   page 13: paragraph translated [44 chars] -> [47 chars]
[02:50:05]   page 13: paragraph translated [56 chars] -> [66 chars]
[02:50:08]   page 13: paragraph translated [61 chars] -> [96 chars]
[02:50:10]   page 13: paragraph translated [54 chars] -> [55 chars]
[02:50:12]   page 13: paragraph translated [58 chars] -> [80 chars]
[02:50:15]   page 13: paragraph translated [57 chars] -> [61 chars]
[02:50:17]   page 13: paragraph translated [54 chars] -> [66 chars]
[02:50:20]   page 13: paragraph translated [60 chars] -> [92 chars]
[02:50:22]   page 13: paragraph translated [58 chars] -> [56 chars]
[02:50:24]   page 13: paragraph translated [40 chars] -> [44 chars]
[02:50:27]   page 13: paragraph translated [56 chars] -> [44 chars]
[02:50:29]   page 13: paragraph translated [62 chars] -> [76 chars]
[02:50:33]   page 13: paragraph translated [238 chars] -> [194 chars]
[02:50:38]   page 13: paragraph translated [469 chars] -> [423 chars]
[02:50:42]   page 13: paragraph translated [222 chars] -> [213 chars]
[02:50:44]   page 13: paragraph translated [56 chars] -> [68 chars]
[02:50:48]   page 13: paragraph translated [317 chars] -> [338 chars]
[02:50:51]   page 13: paragraph translated [192 chars] -> [183 chars]
[02:50:54]   region content rescaled to 0.88x to fit
[02:50:55] --- page 14 (14/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:51:02]   page 14: paragraph translated [635 chars] -> [592 chars]
[02:51:12]   page 14: paragraph translated [1105 chars] -> [924 chars]
[02:51:16]   page 14: paragraph translated [205 chars] -> [190 chars]
[02:51:20]   page 14: paragraph translated [315 chars] -> [348 chars]
[02:51:22]   page 14: paragraph translated [48 chars] -> [46 chars]
[02:51:25]   page 14: paragraph translated [63 chars] -> [77 chars]
[02:51:27]   page 14: paragraph translated [57 chars] -> [64 chars]
[02:51:30]   page 14: paragraph translated [66 chars] -> [84 chars]
[02:51:32]   page 14: paragraph translated [51 chars] -> [50 chars]
[02:51:34]   page 14: paragraph translated [44 chars] -> [38 chars]
[02:51:36]   page 14: paragraph translated [63 chars] -> [70 chars]
[02:51:39]   page 14: paragraph translated [59 chars] -> [89 chars]
[02:51:42]   page 14: paragraph translated [58 chars] -> [56 chars]
[02:51:44]   page 14: paragraph translated [61 chars] -> [77 chars]
[02:51:47]   page 14: paragraph translated [61 chars] -> [47 chars]
[02:51:49]   page 14: paragraph translated [61 chars] -> [56 chars]
[02:51:51]   page 14: paragraph translated [51 chars] -> [53 chars]
[02:51:54]   page 14: paragraph translated [62 chars] -> [82 chars]
[02:51:56]   page 14: paragraph translated [65 chars] -> [61 chars]
[02:51:59]   page 14: paragraph translated [66 chars] -> [86 chars]
[02:52:01]   page 14: paragraph translated [65 chars] -> [40 chars]
[02:52:03]   page 14: paragraph translated [54 chars] -> [58 chars]
[02:52:06]   page 14: paragraph translated [62 chars] -> [125 chars]
[02:52:08]   page 14: paragraph translated [62 chars] -> [65 chars]
[02:52:12]   page 14: paragraph translated [191 chars] -> [216 chars]
[02:52:15]   page 14: paragraph translated [80 chars] -> [89 chars]
[02:52:17]   page 14: paragraph translated [46 chars] -> [33 chars]
[02:52:19]   page 14: paragraph translated [44 chars] -> [48 chars]
[02:52:24]   page 14: paragraph translated [341 chars] -> [325 chars]
[02:52:26]   page 14: paragraph translated [30 chars] -> [25 chars]
[02:52:28]   page 14: paragraph translated [51 chars] -> [49 chars]
[02:52:31]   page 14: paragraph translated [169 chars] -> [150 chars]
[02:52:33]   page 14: paragraph translated [55 chars] -> [73 chars]
[02:52:39]   OVERFLOW: region content overflows by 392pt even at 0.72x font scale -- some text may be clipped
[02:52:42] --- page 15 (15/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:52:44]   page 15: paragraph translated [163 chars] -> [141 chars]
[02:52:56]   page 15: paragraph translated [1220 chars] -> [1107 chars]
[02:53:01]   page 15: paragraph translated [299 chars] -> [256 chars]
[02:53:01]   page 15: skipped near-empty paragraph [1 chars] -> skipped (too little content)
[02:53:03]   page 15: paragraph translated [81 chars] -> [78 chars]
[02:53:08]   page 15: paragraph translated [502 chars] -> [464 chars]
[02:53:11]   page 15: paragraph translated [123 chars] -> [123 chars]
[02:53:13]   page 15: paragraph translated [53 chars] -> [52 chars]
[02:53:17]   page 15: paragraph translated [227 chars] -> [211 chars]
[02:53:23]   page 15: paragraph translated [368 chars] -> [436 chars]
[02:53:27]   page 15: paragraph translated [292 chars] -> [285 chars]
[02:53:36]   page 15: paragraph translated [807 chars] -> [711 chars]
[02:53:40]   page 15: paragraph translated [198 chars] -> [222 chars]
[02:53:43]   page 15: paragraph translated [241 chars] -> [225 chars]
[02:53:45] --- page 16 (16/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:54:04]   page 16: paragraph translated [2254 chars] -> [1997 chars]
[02:54:13]   page 16: paragraph translated [720 chars] -> [668 chars]
[02:54:28]   page 16: paragraph translated [1638 chars] -> [1482 chars]
[02:54:30] --- page 17 (17/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:54:32]   page 17: paragraph translated [98 chars] -> [100 chars]
[02:54:37]   page 17: paragraph translated [438 chars] -> [385 chars]
[02:54:41]   page 17: paragraph translated [303 chars] -> [269 chars]
[02:54:51]   page 17: paragraph translated [986 chars] -> [803 chars]
[02:54:55]   page 17: paragraph translated [299 chars] -> [317 chars]
[02:55:06]   page 17: paragraph translated [1074 chars] -> [925 chars]
[02:55:16]   page 17: paragraph translated [940 chars] -> [849 chars]
[02:55:21]   page 17: paragraph translated [303 chars] -> [277 chars]
[02:55:22] --- page 18 (18/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:55:30]   page 18: paragraph translated [926 chars] -> [666 chars]
[02:55:43]   page 18: paragraph translated [1279 chars] -> [1068 chars]
[02:55:46]   page 18: paragraph translated [125 chars] -> [115 chars]
[02:55:50]   page 18: paragraph translated [279 chars] -> [244 chars]
[02:56:06]   page 18: paragraph translated [1489 chars] -> [1213 chars]
[02:56:12]   page 18: paragraph translated [540 chars] -> [492 chars]
[02:56:13] --- page 19 (19/35) --- 9 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:56:23]   page 19: paragraph translated [1002 chars] -> [813 chars]
[02:56:35]   page 19: paragraph translated [1091 chars] -> [1037 chars]
[02:56:39]   page 19: paragraph translated [256 chars] -> [202 chars]
[02:56:42]   page 19: paragraph translated [250 chars] -> [110 chars]
[02:56:44]   page 19: paragraph translated [14 chars] -> [14 chars]
[02:56:44]   region content rescaled to 0.92x to fit
[02:56:48]   page 19: paragraph translated [179 chars] -> [205 chars]
[02:57:03]   page 19: paragraph translated [1334 chars] -> [1094 chars]
[02:57:05]   page 19: paragraph translated [17 chars] -> [21 chars]
[02:57:05]   region content rescaled to 0.80x to fit
[02:57:10]   page 19: paragraph translated [317 chars] -> [292 chars]
[02:57:10]   region content rescaled to 0.96x to fit
[02:57:12]   page 19: paragraph translated [9 chars] -> [11 chars]
[02:57:14]   page 19: paragraph translated [59 chars] -> [39 chars]
[02:57:14]   region content rescaled to 0.84x to fit
[02:57:14]   WARNING: paragraph rendered at 0.85x -- measure_height disagreed with the actual render
[02:57:15] --- page 20 (20/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:57:32]   page 20: paragraph translated [1623 chars] -> [1277 chars]
[02:57:41]   page 20: paragraph translated [737 chars] -> [705 chars]
[02:57:52]   page 20: paragraph translated [809 chars] -> [793 chars]
[02:58:06]   page 20: paragraph translated [1189 chars] -> [1021 chars]
[02:58:10]   page 20: paragraph translated [304 chars] -> [273 chars]
[02:58:11] --- page 21 (21/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:58:15]   page 21: paragraph translated [256 chars] -> [191 chars]
[02:58:24]   page 21: paragraph translated [828 chars] -> [777 chars]
[02:58:36]   page 21: paragraph translated [1203 chars] -> [968 chars]
[02:58:43]   page 21: paragraph translated [462 chars] -> [454 chars]
[02:58:51]   page 21: paragraph translated [705 chars] -> [655 chars]
[02:59:02]   page 21: paragraph translated [1085 chars] -> [1002 chars]
[02:59:04]   page 21: paragraph translated [59 chars] -> [100 chars]
[02:59:06] --- page 22 (22/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[02:59:22]   page 22: paragraph translated [1107 chars] -> [1038 chars]
[02:59:38]   page 22: paragraph translated [970 chars] -> [804 chars]
[02:59:43]   page 22: paragraph translated [316 chars] -> [299 chars]
[03:00:08]   page 22: paragraph translated [1662 chars] -> [1558 chars]
[03:00:17]   page 22: paragraph translated [644 chars] -> [624 chars]
[03:00:18] --- page 23 (23/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:00:34]   page 23: paragraph translated [1535 chars] -> [1385 chars]
[03:00:38]   page 23: paragraph translated [320 chars] -> [342 chars]
[03:00:44]   page 23: paragraph translated [488 chars] -> [436 chars]
[03:00:46]   page 23: paragraph translated [59 chars] -> [68 chars]
[03:01:09]   page 23: paragraph translated [2308 chars] -> [2096 chars]
[03:01:10] --- page 24 (24/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:01:17]   page 24: paragraph translated [626 chars] -> [601 chars]
[03:01:35]   page 24: paragraph translated [1739 chars] -> [1447 chars]
[03:01:40]   page 24: paragraph translated [479 chars] -> [434 chars]
[03:01:46]   page 24: paragraph translated [450 chars] -> [447 chars]
[03:01:50]   page 24: paragraph translated [213 chars] -> [188 chars]
[03:01:59]   page 24: paragraph translated [889 chars] -> [788 chars]
[03:02:03]   page 24: paragraph translated [236 chars] -> [222 chars]
[03:02:04] --- page 25 (25/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:02:27]   page 25: paragraph translated [2371 chars] -> [1923 chars]
[03:02:37]   page 25: paragraph translated [854 chars] -> [821 chars]
[03:02:52]   page 25: paragraph translated [1350 chars] -> [1152 chars]
[03:02:53] --- page 26 (26/35) --- 37 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:02:56]   page 26: paragraph translated [109 chars] -> [113 chars]
[03:02:56]   region content rescaled to 0.92x to fit
[03:02:58]   page 26: paragraph translated [39 chars] -> [37 chars]
[03:03:00]   page 26: paragraph translated [58 chars] -> [49 chars]
[03:03:00]   region content rescaled to 0.84x to fit
[03:03:03]   page 26: paragraph translated [29 chars] -> [33 chars]
[03:03:03]   region content rescaled to 0.88x to fit
[03:03:05]   page 26: paragraph translated [62 chars] -> [94 chars]
[03:03:06]   region content rescaled to 0.72x to fit
[03:03:08]   page 26: paragraph translated [35 chars] -> [38 chars]
[03:03:08]   region content rescaled to 0.72x to fit
[03:03:08]   page 26: skipped near-empty paragraph [3 chars] -> skipped (too little content)
[03:03:08]   region content rescaled to 0.72x to fit
[03:03:12]   page 26: paragraph translated [168 chars] -> [153 chars]
[03:03:12]   region content rescaled to 0.84x to fit
[03:03:13]   page 26: paragraph translated [38 chars] -> [42 chars]
[03:03:14]   region content rescaled to 0.88x to fit
[03:03:18]   page 26: paragraph translated [241 chars] -> [254 chars]
[03:03:18]   region content rescaled to 0.96x to fit
[03:03:20]   page 26: paragraph translated [39 chars] -> [40 chars]
[03:03:20]   region content rescaled to 0.92x to fit
[03:03:22]   page 26: paragraph translated [61 chars] -> [48 chars]
[03:03:22]   region content rescaled to 0.80x to fit
[03:03:25]   page 26: paragraph translated [49 chars] -> [46 chars]
[03:03:25]   region content rescaled to 0.80x to fit
[03:03:25]   page 26: skipped near-empty paragraph [3 chars] -> skipped (too little content)
[03:03:25]   region content rescaled to 0.92x to fit
[03:03:29]   page 26: paragraph translated [243 chars] -> [241 chars]
[03:03:29]   region content rescaled to 0.96x to fit
[03:03:31]   page 26: paragraph translated [47 chars] -> [53 chars]
[03:03:31]   region content rescaled to 0.76x to fit
[03:03:33]   page 26: paragraph translated [12 chars] -> [10 chars]
[03:03:34]   region content rescaled to 0.92x to fit
[03:03:36]   page 26: paragraph translated [114 chars] -> [136 chars]
[03:03:37]   region content rescaled to 0.92x to fit
[03:03:39]   page 26: paragraph translated [20 chars] -> [22 chars]
[03:03:39]   region content rescaled to 0.84x to fit
[03:03:43]   page 26: paragraph translated [363 chars] -> [325 chars]
[03:03:46]   page 26: paragraph translated [46 chars] -> [45 chars]
[03:03:46]   region content rescaled to 0.80x to fit
[03:03:48]   page 26: paragraph translated [9 chars] -> [10 chars]
[03:03:48]   region content rescaled to 0.88x to fit
[03:03:51]   page 26: paragraph translated [185 chars] -> [181 chars]
[03:03:52]   region content rescaled to 0.92x to fit
[03:03:54]   page 26: paragraph translated [51 chars] -> [45 chars]
[03:03:54]   region content rescaled to 0.88x to fit
[03:03:57]   page 26: paragraph translated [117 chars] -> [129 chars]
[03:03:57]   region content rescaled to 0.92x to fit
[03:03:59]   page 26: paragraph translated [81 chars] -> [71 chars]
[03:04:01]   page 26: paragraph translated [14 chars] -> [15 chars]
[03:04:09]   page 26: paragraph translated [823 chars] -> [655 chars]
[03:04:11]   page 26: paragraph translated [34 chars] -> [27 chars]
[03:04:11]   region content rescaled to 0.92x to fit
[03:04:13]   page 26: paragraph translated [21 chars] -> [28 chars]
[03:04:13]   region content rescaled to 0.88x to fit
[03:04:16]   page 26: paragraph translated [44 chars] -> [45 chars]
[03:04:16]   region content rescaled to 0.80x to fit
[03:04:16]   page 26: skipped near-empty paragraph [3 chars] -> skipped (too little content)
[03:04:22]   page 26: paragraph translated [538 chars] -> [477 chars]
[03:04:25]   page 26: paragraph translated [173 chars] -> [165 chars]
[03:04:27]   page 26: paragraph translated [17 chars] -> [13 chars]
[03:04:27]   region content rescaled to 0.92x to fit
[03:04:32]   page 26: paragraph translated [300 chars] -> [290 chars]
[03:04:32]   region content rescaled to 0.96x to fit
[03:04:34] --- page 27 (27/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:04:40]   page 27: paragraph translated [614 chars] -> [522 chars]
[03:04:49]   page 27: paragraph translated [911 chars] -> [779 chars]
[03:04:52]   page 27: paragraph translated [232 chars] -> [221 chars]
[03:04:59]   page 27: paragraph translated [616 chars] -> [590 chars]
[03:05:02]   page 27: paragraph translated [84 chars] -> [127 chars]
[03:05:11]   page 27: paragraph translated [886 chars] -> [789 chars]
[03:05:19]   page 27: paragraph translated [660 chars] -> [583 chars]
[03:05:27]   page 27: paragraph translated [675 chars] -> [632 chars]
[03:05:28] --- page 28 (28/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:05:52]   page 28: paragraph translated [2415 chars] -> [2174 chars]
[03:06:01]   page 28: paragraph translated [802 chars] -> [712 chars]
[03:06:09]   page 28: paragraph translated [722 chars] -> [682 chars]
[03:06:19]   page 28: paragraph translated [879 chars] -> [695 chars]
[03:06:21] --- page 29 (29/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:06:39]   page 29: paragraph translated [1623 chars] -> [1455 chars]
[03:06:50]   page 29: paragraph translated [615 chars] -> [534 chars]
[03:06:53]   page 29: paragraph translated [59 chars] -> [59 chars]
[03:06:55]   page 29: paragraph translated [57 chars] -> [47 chars]
[03:06:59]   page 29: paragraph translated [169 chars] -> [184 chars]
[03:07:01]   page 29: paragraph translated [12 chars] -> [11 chars]
[03:07:05]   page 29: paragraph translated [169 chars] -> [209 chars]
[03:07:08]   page 29: paragraph translated [108 chars] -> [120 chars]
[03:07:12]   page 29: paragraph translated [10 chars] -> [176 chars]
[03:07:14]   page 29: paragraph translated [6 chars] -> [4 chars]
[03:07:17]   page 29: paragraph translated [114 chars] -> [116 chars]
[03:07:22]   page 29: paragraph translated [15 chars] -> [259 chars]
[03:07:26]   page 29: paragraph translated [100 chars] -> [120 chars]
[03:07:30]   page 29: paragraph translated [218 chars] -> [208 chars]
[03:07:33]   page 29: paragraph translated [56 chars] -> [65 chars]
[03:07:38]   page 29: paragraph translated [223 chars] -> [238 chars]
[03:07:41]   page 29: paragraph translated [121 chars] -> [99 chars]
[03:07:44]   page 29: paragraph translated [76 chars] -> [74 chars]
[03:07:47]   page 29: paragraph translated [58 chars] -> [157 chars]
[03:07:53]   page 29: paragraph translated [318 chars] -> [305 chars]
[03:07:58]   page 29: paragraph translated [199 chars] -> [184 chars]
[03:08:01]   page 29: paragraph translated [113 chars] -> [100 chars]
[03:08:02]   region content rescaled to 0.92x to fit
[03:08:04] --- page 30 (30/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:08:08]   page 30: paragraph translated [243 chars] -> [237 chars]
[03:08:15]   page 30: paragraph translated [445 chars] -> [441 chars]
[03:08:36]   page 30: paragraph translated [1620 chars] -> [1366 chars]
[03:08:38]   page 30: paragraph translated [58 chars] -> [82 chars]
[03:08:41]   page 30: paragraph translated [65 chars] -> [71 chars]
[03:08:44]   page 30: paragraph translated [49 chars] -> [43 chars]
[03:08:46]   page 30: paragraph translated [60 chars] -> [76 chars]
[03:08:48]   page 30: paragraph translated [58 chars] -> [53 chars]
[03:08:50]   page 30: paragraph translated [59 chars] -> [56 chars]
[03:08:53]   page 30: paragraph translated [60 chars] -> [49 chars]
[03:08:55]   page 30: paragraph translated [36 chars] -> [80 chars]
[03:08:58]   page 30: paragraph translated [62 chars] -> [75 chars]
[03:09:01]   page 30: paragraph translated [62 chars] -> [63 chars]
[03:09:03]   page 30: paragraph translated [48 chars] -> [39 chars]
[03:09:05]   page 30: paragraph translated [59 chars] -> [66 chars]
[03:09:08]   page 30: paragraph translated [62 chars] -> [57 chars]
[03:09:11]   page 30: paragraph translated [57 chars] -> [82 chars]
[03:09:13]   page 30: paragraph translated [73 chars] -> [65 chars]
[03:09:20]   page 30: paragraph translated [523 chars] -> [436 chars]
[03:09:23]   page 30: paragraph translated [115 chars] -> [152 chars]
[03:09:28]   page 30: paragraph translated [324 chars] -> [323 chars]
[03:09:35]   page 30: paragraph translated [432 chars] -> [393 chars]
[03:09:36]   region content rescaled to 0.92x to fit
[03:09:38] --- page 31 (31/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:09:44]   page 31: paragraph translated [431 chars] -> [390 chars]
[03:10:06]   page 31: paragraph translated [1866 chars] -> [1783 chars]
[03:10:13]   page 31: paragraph translated [665 chars] -> [586 chars]
[03:10:24]   page 31: paragraph translated [1155 chars] -> [928 chars]
[03:10:31]   page 31: paragraph translated [541 chars] -> [483 chars]
[03:10:32] --- page 32 (32/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:10:38]   page 32: paragraph translated [493 chars] -> [437 chars]
[03:10:40]   page 32: paragraph translated [13 chars] -> [5 chars]
[03:10:50]   page 32: paragraph translated [1035 chars] -> [752 chars]
[03:11:05]   page 32: paragraph translated [1299 chars] -> [1163 chars]
[03:11:08]   page 32: paragraph translated [74 chars] -> [72 chars]
[03:11:11]   page 32: paragraph translated [75 chars] -> [77 chars]
[03:11:13]   page 32: paragraph translated [71 chars] -> [57 chars]
[03:11:16]   page 32: paragraph translated [73 chars] -> [84 chars]
[03:11:18]   page 32: paragraph translated [17 chars] -> [17 chars]
[03:11:34]   page 32: paragraph translated [1301 chars] -> [1075 chars]
[03:11:46]   page 32: paragraph translated [888 chars] -> [800 chars]
[03:11:57]   page 32: paragraph translated [617 chars] -> [609 chars]
[03:11:59] --- page 33 (33/35) --- 26 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:12:02]   page 33: paragraph translated [46 chars] -> [59 chars]
[03:12:04]   page 33: paragraph translated [10 chars] -> [12 chars]
[03:12:04]   region content rescaled to 0.92x to fit
[03:12:06]   page 33: WARNING: output is much shorter than the source (probable truncation) (paragraph: 'schaftlicher Sachverhalt, kein Naturgesetz. Sie kann slch Je')
[03:12:06]   page 33: paragraph translated [293 chars] -> [22 chars]
[03:12:09]   page 33: paragraph translated [50 chars] -> [50 chars]
[03:12:09]   region content rescaled to 0.92x to fit
[03:12:15]   page 33: paragraph translated [433 chars] -> [474 chars]
[03:12:15]   region content rescaled to 0.96x to fit
[03:12:18]   page 33: paragraph translated [33 chars] -> [44 chars]
[03:12:18]   region content rescaled to 0.80x to fit
[03:12:21]   page 33: paragraph translated [148 chars] -> [143 chars]
[03:12:21]   region content rescaled to 0.92x to fit
[03:12:23]   page 33: paragraph translated [22 chars] -> [23 chars]
[03:12:26]   page 33: paragraph translated [73 chars] -> [55 chars]
[03:12:26]   region content rescaled to 0.80x to fit
[03:12:29]   page 33: paragraph translated [39 chars] -> [42 chars]
[03:12:35]   page 33: paragraph translated [512 chars] -> [435 chars]
[03:12:37]   page 33: paragraph translated [37 chars] -> [35 chars]
[03:12:39]   page 33: paragraph translated [75 chars] -> [71 chars]
[03:12:40]   region content rescaled to 0.80x to fit
[03:12:42]   page 33: paragraph translated [57 chars] -> [63 chars]
[03:12:42]   region content rescaled to 0.72x to fit
[03:12:44]   page 33: paragraph translated [8 chars] -> [12 chars]
[03:12:45]   OVERFLOW: region content overflows by 5pt even at 0.72x font scale -- some text may be clipped
[03:12:50]   page 33: paragraph translated [427 chars] -> [482 chars]
[03:12:50]   region content rescaled to 0.96x to fit
[03:12:52]   page 33: paragraph translated [38 chars] -> [31 chars]
[03:12:52]   region content rescaled to 0.88x to fit
[03:12:54]   page 33: paragraph translated [9 chars] -> [9 chars]
[03:12:55]   region content rescaled to 0.88x to fit
[03:12:57]   page 33: paragraph translated [75 chars] -> [58 chars]
[03:12:57]   region content rescaled to 0.80x to fit
[03:12:59]   page 33: paragraph translated [44 chars] -> [34 chars]
[03:13:03]   page 33: paragraph translated [238 chars] -> [221 chars]
[03:13:03]   region content rescaled to 0.92x to fit
[03:13:05]   page 33: paragraph translated [45 chars] -> [36 chars]
[03:13:06]   region content rescaled to 0.88x to fit
[03:13:08]   page 33: paragraph translated [6 chars] -> [12 chars]
[03:13:08]   OVERFLOW: region content overflows by 6pt even at 0.72x font scale -- some text may be clipped
[03:13:11]   page 33: paragraph translated [154 chars] -> [151 chars]
[03:13:14]   page 33: paragraph translated [141 chars] -> [148 chars]
[03:13:14]   region content rescaled to 0.96x to fit
[03:13:17]   page 33: paragraph translated [73 chars] -> [69 chars]
[03:13:19]   page 33: paragraph translated [78 chars] -> [87 chars]
[03:13:21]   page 33: paragraph translated [73 chars] -> [82 chars]
[03:13:24]   page 33: paragraph translated [42 chars] -> [43 chars]
[03:13:26]   page 33: paragraph translated [81 chars] -> [84 chars]
[03:13:29]   page 33: paragraph translated [74 chars] -> [102 chars]
[03:13:32]   page 33: paragraph translated [80 chars] -> [72 chars]
[03:13:34]   page 33: paragraph translated [73 chars] -> [90 chars]
[03:13:37]   page 33: paragraph translated [79 chars] -> [93 chars]
[03:13:39]   page 33: paragraph translated [78 chars] -> [57 chars]
[03:13:42]   page 33: paragraph translated [71 chars] -> [64 chars]
[03:13:45]   page 33: paragraph translated [81 chars] -> [113 chars]
[03:13:48]   page 33: paragraph translated [72 chars] -> [82 chars]
[03:13:54]   page 33: paragraph translated [512 chars] -> [514 chars]
[03:13:59]   page 33: paragraph translated [322 chars] -> [274 chars]
[03:14:07]   page 33: paragraph translated [659 chars] -> [603 chars]
[03:14:15]   page 33: paragraph translated [816 chars] -> [731 chars]
[03:14:16]   region content rescaled to 0.96x to fit
[03:14:19] --- page 34 (34/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:14:21]   page 34: paragraph translated [89 chars] -> [87 chars]
[03:14:26]   page 34: paragraph translated [132 chars] -> [132 chars]
[03:14:33]   page 34: paragraph translated [685 chars] -> [625 chars]
[03:14:38]   page 34: paragraph translated [361 chars] -> [314 chars]
[03:14:43]   page 34: paragraph translated [329 chars] -> [295 chars]
[03:14:48]   page 34: paragraph translated [210 chars] -> [223 chars]
[03:14:53]   page 34: paragraph translated [441 chars] -> [395 chars]
[03:15:00]   page 34: paragraph translated [532 chars] -> [494 chars]
[03:15:04]   page 34: paragraph translated [234 chars] -> [218 chars]
[03:15:05]   page 34: paragraph translated [64 chars] -> [58 chars]
[03:15:08]   page 34: paragraph translated [64 chars] -> [70 chars]
[03:15:11]   page 34: paragraph translated [77 chars] -> [66 chars]
[03:15:14]   page 34: paragraph translated [62 chars] -> [60 chars]
[03:15:16]   page 34: paragraph translated [73 chars] -> [93 chars]
[03:15:19]   page 34: paragraph translated [73 chars] -> [69 chars]
[03:15:21]   page 34: paragraph translated [80 chars] -> [89 chars]
[03:15:24]   page 34: paragraph translated [63 chars] -> [89 chars]
[03:15:26]   page 34: paragraph translated [62 chars] -> [58 chars]
[03:15:29]   page 34: paragraph translated [77 chars] -> [94 chars]
[03:15:32]   page 34: paragraph translated [69 chars] -> [61 chars]
[03:15:34]   page 34: paragraph translated [55 chars] -> [86 chars]
[03:15:37]   page 34: paragraph translated [41 chars] -> [49 chars]
[03:15:39]   page 34: paragraph translated [66 chars] -> [84 chars]
[03:15:43]   page 34: paragraph translated [68 chars] -> [116 chars]
[03:15:45]   page 34: paragraph translated [72 chars] -> [63 chars]
[03:15:47]   page 34: paragraph translated [60 chars] -> [62 chars]
[03:15:50]   page 34: paragraph translated [59 chars] -> [66 chars]
[03:15:53]   page 34: paragraph translated [131 chars] -> [153 chars]
[03:15:58]   page 34: paragraph translated [308 chars] -> [314 chars]
[03:16:03]   page 34: paragraph translated [243 chars] -> [255 chars]
[03:16:07]   page 34: paragraph translated [222 chars] -> [247 chars]
[03:16:13]   page 34: paragraph translated [506 chars] -> [488 chars]
[03:16:19]   page 34: paragraph translated [471 chars] -> [440 chars]
[03:16:25]   OVERFLOW: region content overflows by 321pt even at 0.72x font scale -- some text may be clipped
[03:16:27] --- page 35 (35/35) --- 2 region(s), 2 column(s), 0 obstacle(s), 0 table(s)
[03:16:35]   page 35: paragraph translated [546 chars] -> [541 chars]
[03:16:39]   page 35: paragraph translated [126 chars] -> [118 chars]
[03:16:45]   page 35: paragraph translated [570 chars] -> [549 chars]
[03:16:50]   page 35: paragraph translated [153 chars] -> [151 chars]
[03:16:58]   page 35: paragraph translated [803 chars] -> [707 chars]
[03:17:04]   page 35: paragraph translated [345 chars] -> [316 chars]
[03:17:09]   page 35: paragraph translated [337 chars] -> [323 chars]
[03:17:13]   page 35: paragraph translated [229 chars] -> [235 chars]
[03:17:18]   page 35: paragraph translated [415 chars] -> [383 chars]
[03:17:25]   page 35: paragraph translated [589 chars] -> [492 chars]
[03:17:33]   page 35: paragraph translated [577 chars] -> [599 chars]
[03:17:35]   translated document title: "'Untitled'" -> 'Untitled'
[03:17:38] done: agnoli_der-staat-des-kapitals-8cf1996ef9_ocr.pdf -> agnoli_der-staat-des-kapitals-8cf1996ef9_ocr_en.pdf
```
