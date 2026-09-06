# AGENTS.md — `REPLACE_BOOK_TITLE/`

This folder is a spoiler-bounded reading workspace. The rules below are binding for
every discussion and every note in it, at any depth.

- The hard boundary is `.reading/bookmark.json`. Nothing about this book may be
  said, foreshadowed or speculated past it — in page order, not chronological order.
  The whole book is in the model's training data; that is the leak this guards.
- **Before discussing any content, load the `reading-buddy` skill and run `status`.**
  The full protocol lives there. This file is only the always-on floor.
- Never hand-edit `.reading/bookmark.json`; it is runtime-owned state.
- `.reading/summary.json` is the agent's running memory — runtime-owned like
  the bookmark. Never hand-edit it, and treat its entries as claims to verify
  against the original text, not facts.
- Never grep or open `source/` directly, nor any annotation file or index that
  crosses the bookmark. Query through the runtime, which serves only what is read.
- Default to description, not interpretation. No themes, verdicts, or forward
  framing unless explicitly asked — and then still bounded.

Commands run from this folder:

    python3 REPLACE_SKILL_RUNTIME_PATH/read.py status

## This edition

REPLACE — the edition being read, any pagination quirk, and the annotation source
if the book has one. Book-specific configuration lives in `.reading/book.json`.
