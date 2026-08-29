# auto-translate-pdf — what we've done, in plain terms

This is a recap of the whole journey, written for a human reading back over
it later — not a technical reference. For that, see:
- **README.md** — what the tool does, how to set it up and use it, the
  pipeline mechanics, current known limitations
- **AUDIT_FIXES.md** — the full, unabridged bug-fix history (five audit
  rounds' worth), with before/after evidence for each fix
- **CLAUDE.md** — working notes/ground rules for anyone (human or AI)
  continuing to develop this codebase

## The idea

You wanted your German-language academic PDF collection (Adorno, Horkheimer,
and similar) readable in English — not just a rough machine translation, but
one that actually looks like the original document: same paragraphs, same
footnotes, same italics for book titles, same page layout. And you wanted it
to run entirely on your own machine, no cloud API, no per-document cost.

## Building the pipeline

We picked **TranslateGemma 4B** (Google's Gemma 3, fine-tuned specifically
for translation) running via **mlx-lm** — Apple Silicon's native ML
framework — so it runs fully offline on your M1 Air, no API key, no
recurring cost.

The hard part was never getting a translation out of the model; it was
making the *layout* survive the trip. A PDF doesn't store "paragraphs" —
it stores lines of text at specific coordinates, and different PDF
producers structure that differently underneath. Getting English text back
into the same visual shape as the German original meant building real
paragraph detection (not just "translate each line"), a page reflow system
that pushes later content up or down as translated text runs shorter or
longer than the original, and specific handling for footnote markers and
italics — since a translation model only ever sees a flat string, and any
formatting has to be smuggled through as plain-text markers and
reconstructed afterward.

## Making it hands-off

Once the pipeline worked standalone, we wrapped it as a **macOS Folder
Action**: drop a PDF into `~/Translate`, and it gets translated
automatically in the background, matching the same pattern as your other
tools (auto-OCR, auto-rename-by-DOI). No need to open a terminal for routine
use.

## Fixing real bugs

Running the pipeline against your actual documents (not just the clean
sample we built it against) surfaced real problems: paragraphs shattering
into disconnected fragments on PDFs with an unusual internal structure, a
945MB output file from a font-embedding bug, the model occasionally
returning a confused non-answer that got inserted into the PDF as if it
were real content, and — the one you actually flagged — badly oversized
gaps between footnote entries.

That footnote-gap investigation is worth calling out because of how it
played out: the first fix (recomputing the gap-preservation math) helped,
but a **line-by-line external audit of the whole codebase** later found the
*real* root cause — a CSS rule that was silently losing to PyMuPDF's
default paragraph margin, adding a phantom blank space to *every single
paragraph* in the document, not just footnotes. That one audit fix
resolved both the footnote-gap issue and a title-page spacing problem
we'd separately (and incorrectly) diagnosed as a width-measurement bug
weeks earlier. Sometimes the "unrelated" bug and the "already fixed" bug
are the same bug wearing two different symptoms.

## Publishing it

The tool went up as a public GitHub repo —
**[github.com/materialcritic/auto-translate-pdf](https://github.com/materialcritic/auto-translate-pdf)**
— with the translated PDFs, model weights, and your local paths deliberately
left out (the translated output was someone else's copyrighted academic
article; no business being in a code repo).

From there, further audit rounds (documented in full in `AUDIT_FIXES.md`)
found and fixed more issues, and the pipeline grew real support for
multi-column layouts, embedded images/figures, and tables — well beyond
where it started. If you want the full blow-by-blow of that later work,
`AUDIT_FIXES.md` is the place; it's substantial.

## Running it without a Mac

Since the local pipeline needs Apple Silicon (`mlx-lm` is Mac-only), we
built a **Colab notebook** as a fallback — same pipeline, same paragraph/
footnote/italic handling, but swapping in `transformers` + 4-bit
`bitsandbytes` on a free Colab GPU instead of MLX. Getting that working
end-to-end took a few rounds of real troubleshooting:

- The model is gated behind Google's Gemma license — needed a Hugging Face
  login step in the notebook.
- Bumping up to the 12B model (to use the T4's spare GPU headroom) crashed
  the session — turned out to be a **system RAM** problem, not GPU VRAM: a
  free-tier Colab runtime only gets ~12.7GB of CPU RAM, and loading a large
  checkpoint by default stages the *entire* thing in CPU RAM before moving
  it to the GPU. Fixed with `low_cpu_mem_usage=True`.
- Even with that fix, the *download* itself could crash the runtime — an
  earlier "speed up downloads" setting (`HF_XET_HIGH_PERFORMANCE`) turned
  out to buffer more in RAM in exchange for faster transfer, a bad
  tradeoff on Colab's constrained memory. You ultimately traded the 12B
  model and the faster download back for stability and stuck with 4B,
  which just works.
- Along the way: a stuck "reconnecting" Colab session (needed a full
  runtime disconnect-and-reconnect), and a question about whether the job
  survives your laptop sleeping (short answer: not reliably — `caffeinate
  -s` while plugged in and connected keeps it alive, but closing the lid
  or losing WiFi doesn't care what `caffeinate` thinks).

End result: a 40-page document translated on Colab's free T4 in 49 minutes
— slower than the local Mac pipeline (MLX's native quantized kernels beat
`transformers` + `bitsandbytes` for this workload), but a real fallback
when the Mac isn't available.

## Where things stand

The tool is a working, published, audited, tested piece of software at
this point — not a one-off script. If you're picking this back up after a
break, start with README.md's "Setup" and "Known limitations" sections,
and use `CLAUDE.md` if you (or another Claude session) are going to keep
developing it further.
