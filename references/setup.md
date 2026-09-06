# Setup wizard

Run this when a workspace has no `.reading/book.json`. You are talking to a
reader who may not be technical: ask plain questions one at a time, and do the
technical work yourself. Do not ask them for character offsets, regexes or JSON.

`RB` is this skill's `runtime/` directory. `WS` is the book workspace you are
creating. Everything below runs from `WS`.

## 0. Check the interpreter first

    python3 --version

**3.10 or newer is required.** macOS ships 3.9.6 at `/usr/bin/python3`, and the
runtime fails at import on it — the error is an unhelpful `TypeError` about `|`.
If the version is too old, stop and have them install a current Python
(`brew install python`), then re-check before continuing. Also confirm SQLite FTS5:

    python3 -c "import sqlite3;sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)');print('fts5 ok')"

## 1. Ask about the book

Ask, in this order, and keep the answers:

1. Which book, and which edition are they physically holding (publisher / ISBN if
   they can see it)?
2. What is the **first** printed page number of the main text, and the **last**?
   That becomes `page_range`; the `bookmark` command rejects pages outside it.
3. Are they reading with a translation alongside, or in one language only?

## 2. Get the text files

They must supply the text themselves; this skill ships no corpus.

- Public-domain books: Project Gutenberg plain text is fine.
- Anything in copyright, and any translation: their own ebook, converted to
  plain UTF-8 text. Keep it local and gitignored.

Put them under `WS/source/`. Confirm each file is the *whole* book and that the
two languages are the same work — a mismatch produces silent nonsense later.

**Strip nothing yet.** Front matter is handled by the start anchor in step 5.

## 3. Write `.reading/book.json`

Start from `templates/book.json`. Fill `title`, `documents`, `page_range`, `note`.

Set to `null` anything the book does not have. All of these are genuinely optional
and the runtime degrades cleanly:

- `calibration` — a char-offset ↔ printed-page anchor file. Without it the `page N`
  command returns `page_map_unavailable` and citations carry no page estimate.
  **Everything else still works**, because pages are advisory and the real boundary
  is the exact quotation. Leave it `null`; it can be built later.
- `annotations` — only for books with a page-range-keyed annotation wiki whose files
  are named `Pages_<low>-<high>.txt`. Chapter-keyed annotation sites do not fit this
  schema. Leave it `null` rather than forcing it.
- `alignment` — advisory original↔translation offset history. Harmless to point at a
  path that does not exist yet; it gets written on the first translation sync.
- `part_marker` — a regex matching structural section markers in *both* text files.
  If the book has clean chapter headings, setting this measurably improves
  translation position estimates. If the two languages mark sections differently,
  leave it `null`.

Then verify the descriptor parses before going further:

    python3 $RB/read.py status

Expect `blocked` with a missing-bookmark reason. A `config_invalid` or
`config_missing` error means step 3 is wrong — fix it now, not later.

## 4. Explain the one rule before taking their position

Tell them plainly: from here on you will only ever discuss the book up to the point
they report, and the position is exact down to the sentence. If they stop
mid-sentence, say so and it will be honoured. Getting this understood now is what
makes the rest trustworthy.

## 5. Find the start anchor

The start anchor is the first sentence of the **main text**, past the title page,
contents, prefaces and (in Gutenberg files) the licence header.

You may read the **first few thousand characters** of the text file directly to find
it. This is the one sanctioned direct read: it is page one, already read by
definition. Do not scroll further than needed to locate the body opening.

Pick a distinctive complete phrase of 8–1000 characters from that first sentence.
Read it back to the reader and confirm it matches how their copy opens — editions
differ in what counts as front matter.

## 6. Ask where they are now

Two questions:

- Which page are they on, and did they finish it?
- Looking at their book: what are the **last words they actually read**? Ask them to
  type the sentence out, exactly, including punctuation.

Then commit:

    python3 $RB/read.py bookmark --page N --start "<step 5 phrase>" --after "<their words>"

Add `--page-complete` only if they finished that page.

**Both quotations must be unique in the whole book.** If the command reports the
quotation is ambiguous or not found:

- ambiguous → ask for more of the sentence, or take the sentence before it as well;
- not found → the typed text does not match the file. Usual causes are curly vs
  straight quotes, an em dash, or a hyphen from a line break in the printed book.
  Matching already normalizes case, punctuation and line wrapping, so a genuine
  miss usually means a typo or a different edition. Ask them to re-read it to you
  and try a shorter, cleaner fragment from the middle of the sentence.

Loop until it commits. Then:

    python3 $RB/read.py status

`boundary` should read `exact_text`.

## 7. Anchor the translation

Skip if reading in one language.

The reader reports progress in the original language only, so you locate both
translation anchors yourself. Probe windows are derived from the original position
and are bounded — while no anchor pair exists yet the forward band is 40000
characters, which is generous enough to bootstrap.

    python3 $RB/read.py sync-translation --for start
    python3 $RB/read.py sync-translation --for stop

Each returns at most three windows under `probe.windows`, inside a `zh_search_band`
derived from the original-language position. Recognize the passage corresponding to
the original start and stop, and narrow with `--near "<exact phrase>"` and
`--shift <chars>` when the first windows overshoot or fall short.

Commit both together — a translation start is rejected without its stop:

    python3 $RB/read.py bookmark --translation-start "<first translated words>" --translation-after "<last translated words>"

Omitting `--page`/`--after` deliberately carries the original boundary forward.

Probe content is for locating the anchor. **Never repeat it back as story content.**

## 8. Scaffold the notes

Copy `templates/AGENTS.md` to `WS/AGENTS.md` and fill in:

- the resolved absolute path to `$RB`, so every command in the workspace is
  copy-pasteable;
- the book title and edition from step 1;
- any book-specific facts worth binding (pagination quirks, an annotation source).

Create the note named in `book.json`'s `note` field. The `bookmark` command
refreshes a **Current position** line in it; that line is a one-way projection and
the runtime never reads it back, so editing it changes nothing.

In an Obsidian vault, `note` is vault-relative and `[[wikilinks]]` work normally.
A running-summary note (facts only, page numbers, synced after each sitting) is
worth creating now so the first sitting has somewhere to land.

## 9. Verify end to end

    python3 $RB/read.py status
    python3 $RB/read.py current --max-chars 600
    python3 $RB/read.py search "<a phrase they remember from a recent page>" --mode phrase

All three should succeed, and the last should return the passage they remember.
Then confirm the negative case works: search for something you know is well past
their bookmark, and check it returns `no_matches` rather than content.

Tell them plainly what they now have, and that the only maintenance is reporting
where they stopped after each sitting.
