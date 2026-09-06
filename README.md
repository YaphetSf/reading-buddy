# reading-buddy

An agent skill for reading a long book with an agent that cannot spoil it.

The model has the whole book in its training data. That is the leak this guards
against. Every answer is bounded by an exact quotation of where you actually
stopped — down to mid-sentence — and the retrieval layer is only ever handed the
text slice you have already read. Ranking statistics, snippets and context
expansion cannot see past the bookmark, and a no-match answer never reveals
whether a match exists in the unread part.

Works with an original text, an optional translation alongside it, and an
optional page-range annotation wiki.

## Install

    git clone <this repo> ~/.agents/skills/reading-buddy

It is a plain agent skill — a `SKILL.md` with frontmatter, bundled
references and scripts — so any client that reads agent skills can load it.
Clients that look in their own directory take a symlink:

    ln -s ~/.agents/skills/reading-buddy ~/.claude/skills/reading-buddy

Requires Python 3.10+ with SQLite FTS5. No packages, no network service, no
model API, no persistent database.

## Use

Make a folder for the book, then ask your agent to set it up:

> Help me set up a workspace to read Moby-Dick.

The skill walks you through it: what edition you are holding, where the main
text starts and ends, the text files, and where your bookmark is right now. You
supply the text yourself — this repo ships no book.

After that, each sitting is one sentence: tell it the page and the last words
you read. It updates the bookmark, re-anchors the translation, and can keep a
reading log.

## Layout

    <skill>/runtime/   the app — shared, book-agnostic
    <book>/.reading/   per-book config and state: book.json, bookmark.json
    <book>/notes/      your notes
    <book>/source/     your text files — keep local, keep gitignored

One reader, one book, one workspace. The runtime is shared; nothing about a
specific book lives in it.

## Limits

This is a retrieval interface, not an operating-system sandbox. An agent with
direct filesystem access can bypass it; the workspace `AGENTS.md` is what keeps
it from trying. Page positions are estimates and never authorize access — the
exact quotation anchors do.

See `SKILL.md` for the protocol, `references/runtime.md` for the command
surface, and `references/setup.md` for the setup walkthrough.
