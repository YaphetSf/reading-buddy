# reading-buddy

An agent skill for reading a long book without getting spoiled.

## Install

Clone it into your skills directory:

    git clone <this repo> ~/.agents/skills/reading-buddy

It is a plain agent skill — a `SKILL.md` with bundled references and scripts —
so any client that loads agent skills can use it. If your client looks
somewhere else, symlink it:

    ln -s ~/.agents/skills/reading-buddy ~/.claude/skills/reading-buddy

Requirements: Python 3.10 or newer with SQLite FTS5. Setup checks both before
starting. Nothing else — no packages, no network service, no API keys.

## Use

### Get started

Make a folder for the book and ask your agent to set it up:

> Help me set up a workspace to read Infinite Jest.

It walks you through a few plain questions: which edition you are holding (the
Abacus paperback, say), where the main text starts and ends (pages 3–981), and
whether you are reading with a translation alongside (yes, the Chinese one).
You put the text files in `source/` — your own ebook converted to plain text,
or Project Gutenberg for public-domain books. This repo ships no book content.

If the book has a page-by-page annotation wiki (Infinite Jest does), setup can
attach it; wiki pages unlock as you read.

Starting mid-book is fine. Tell it the page you are on and the exact last words
you read — type the sentence out. It anchors its bookmark there and catches its
own summary up, so even the first chat knows the story so far.

### Day to day

A sitting ends with one sentence:

> I'm on page 130, I stopped right after "\<the exact sentence\>".

It updates the bookmark, re-anchors the translation, appends to the reading
log, and extends the running summary it keeps for itself — so the next chat
starts from a short recap of everything so far instead of re-reading the book.

If it ever falls behind (a long break, a missed report), say:

> Catch your summary up to my bookmark.

It can also keep working notes for you in `notes/` — a running "story so far",
a character list with the Chinese names from your translation, whatever you
find useful.

### Just ask about the book

> Who is Lyle so far?
> What happened at Enfield in the last hundred pages?
> I half-remember a scene involving a phone call — find it.

It searches the text before answering, quotes the passage, and cites where it
came from. By default it describes rather than interprets: no themes, no
character verdicts, no "you'll see later" — interpretation is where spoilers
slip in. Ask for interpretation explicitly and you get it, still bounded by
the bookmark.

## Where things live

    <skill>/runtime/   the program, shared between all books
    <book>/.reading/   per-book config and state (book.json, bookmark.json, summary.json)
    <book>/notes/      your notes
    <book>/source/     your text files — keep local, keep gitignored

One reader, one book, one folder. Nothing about a specific book lives in the
skill itself.

