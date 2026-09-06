"""Resolve exact reading boundaries before handing text to retrieval.

The bookmark.json state file is the single truth. The note position line is a
one-way projection: written on every bookmark update, never read back.
"""

from array import array
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata


POSITION_LINE = re.compile(r"(?m)^\*\*Current position:[^\n]*\*\*$")
PUNCTUATION = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                           "‐": "-", "‑": "-", "–": "-", "—": "-"})


class ReadingError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fold(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().translate(PUNCTUATION).split())


def normalized_spans(text, query):
    """Match normalized text, preserving original Unicode character offsets."""
    needle = fold(query)
    if not needle:
        return
    chars, starts, ends = [], array("Q"), array("Q")
    pos = 0
    while pos < len(text):
        stop = pos + 1
        while stop < len(text) and unicodedata.combining(text[stop]):
            stop += 1
        normalized = unicodedata.normalize("NFKC", text[pos:stop]).casefold().translate(PUNCTUATION)
        for item in normalized:
            if item.isspace():
                if not chars or chars[-1] == " ":
                    if chars:
                        ends[-1] = stop
                    continue
                item = " "
            chars.append(item)
            starts.append(pos)
            ends.append(stop)
        pos = stop
    haystack = "".join(chars)
    offset = 0
    while (at := haystack.find(needle, offset)) >= 0:
        yield starts[at], ends[at + len(needle) - 1]
        offset = at + 1


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_shape(value):
    return (isinstance(value, dict) and set(value) == {"last_en_end", "last_zh_end"}
            and type(value["last_en_end"]) is int and value["last_en_end"] >= 0
            and type(value["last_zh_end"]) is int and value["last_zh_end"] >= 0)


def remember_pair(config, pair):
    """Advisory alignment history for estimates; never a retrieval boundary."""
    if not config.alignment:
        return
    path = config.root / config.alignment
    try:
        pairs = json.loads(path.read_text(encoding="utf-8"))["pairs"]
    except (OSError, ValueError, KeyError, TypeError):
        pairs = None
    if not isinstance(pairs, list):
        pairs = []
    pairs.append(pair)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pairs": pairs}, ensure_ascii=False), encoding="utf-8")


def read_state(config):
    """Raw bookmark state, or an empty dict before the first anchoring."""
    try:
        return json.loads(config.bookmark.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def source_bytes(config, source):
    try:
        raw = (config.root / config.documents[source]).read_bytes()
        return raw, raw.decode("utf-8")
    except (KeyError, OSError, UnicodeError):
        raise ReadingError("source_unavailable", f"Cannot read the {source} source.") from None


def unique_span(text, quote):
    if not isinstance(quote, str) or not 8 <= len(quote.strip()) <= 1000:
        raise ReadingError("invalid_anchor", "Use a distinctive anchor of 8–1000 characters.")
    hits = normalized_spans(text, quote)
    first = next(hits, None)
    if first is None:
        raise ReadingError("anchor_not_found", "Anchor not found; use exact words from your copy.")
    if next(hits, None) is not None:
        # Do not reveal occurrence counts, locations, or surrounding unread text.
        raise ReadingError("ambiguous_anchor", "Anchor is not unique; supply a longer quotation.")
    return first


@dataclass(frozen=True)
class AllowedText:
    source: str
    path: str
    start: int
    end: int
    text: str
    revision: str
    checksum: str


@dataclass(frozen=True)
class Boundary:
    page: int
    page_complete: bool
    documents: dict
    state_checksum: str

    @property
    def wiki_ceiling(self):
        return self.page if self.page_complete else self.page - 1

    def summary(self):
        return {"page": self.page, "page_complete": self.page_complete,
                "boundary": "exact_text", "revision": self.documents["text"].revision}


def load_boundary(config):
    try:
        raw = config.bookmark.read_bytes()
        state = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        raise ReadingError("boundary_missing",
                           "A bookmark is required. Run bookmark with the last read quotation.") from None
    try:
        low, high = config.page_range
        if (type(state.get("version")) is not int or state["version"] != 2
                or type(state["page"]) is not int or not low <= state["page"] <= high
                or type(state["page_complete"]) is not bool
                or not isinstance(state["documents"], dict)
                or "text" not in state["documents"]
                or set(state["documents"]) - set(config.documents)
                or ("translation_sync" in state and not sync_shape(state["translation_sync"]))):
            raise ValueError
        documents = {}
        for source, entry in state["documents"].items():
            raw_source, text = source_bytes(config, source)
            start, end = entry["start"], entry["end"]
            if (entry["sha256"] != digest(raw_source)
                    or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(text)
                    or not isinstance(entry["start_anchor"], str)
                    or not isinstance(entry["end_anchor"], str)
                    or not entry["start_anchor"].strip()
                    or not entry["end_anchor"].strip()):
                raise ValueError
            allowed = text[start:end]
            if (not fold(allowed).startswith(fold(entry["start_anchor"]))
                    or not fold(allowed).endswith(fold(entry["end_anchor"]))):
                raise ValueError
            # The retrieval layer receives only this prefix, never the full document.
            documents[source] = AllowedText(
                source, config.documents[source], start, end, allowed,
                digest(allowed.encode("utf-8"))[:20], entry["sha256"])
        sync = state.get("translation_sync")
        if sync is not None:
            if (sync["last_en_end"] > documents["text"].end
                    or ("translation" in documents
                        and (sync["last_en_end"] != documents["text"].end
                             or sync["last_zh_end"] != documents["translation"].end))):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ReadingError("boundary_stale",
                           "Bookmark metadata or source changed. Re-anchor before retrieving text.") from None
    return Boundary(state["page"], state["page_complete"], documents, digest(raw))


def _atomic_write(path, payload):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                     dir=path.parent, prefix=".bookmark-", delete=False) as f:
        temporary = Path(f.name)
        f.write(payload)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _project(config, page):
    """Best-effort display line in the note; the runtime never reads it back."""
    if not config.note:
        return
    line = f"**Current position: p. {page} — updated {date.today().isoformat()}**"
    path = config.root / config.note
    try:
        text = path.read_text(encoding="utf-8")
        if POSITION_LINE.search(text):
            updated = POSITION_LINE.sub(lambda _: line, text, count=1)
        else:
            updated = text.rstrip("\n") + "\n\n" + line + "\n"
        _atomic_write(path, updated)
    except OSError:
        return  # The projection is cosmetic; a missing note never blocks anything.


def set_bookmark(config, page=None, after=None, start=None, page_complete=False,
                 translation_after=None, translation_start=None):
    """Explicit mutation; ordinary retrieval has no bookmark override.

    Omitting the original --page/--after carries the previous original boundary
    forward, so the translation can be re-anchored on its own. Advancing the
    original without a translation stop drops the translation document (retrieval
    stays disabled) but remembers the last synced pair for sync-translation.
    """
    low, high = config.page_range
    if page is not None and (type(page) is not int or not low <= page <= high):
        raise ReadingError("invalid_page", f"Main-text bookmark must be between pages {low} and {high}.")
    if translation_start and not translation_after:
        raise ReadingError("invalid_anchor", "A translation start also requires its last-read quotation.")
    if config.bookmark.exists():
        previous = read_state(config)
        if not isinstance(previous, dict):
            raise ReadingError("bookmark_invalid", "Cannot read the existing bookmark state.")
        previous_documents = previous.get("documents")
        if not isinstance(previous_documents, dict):
            raise ReadingError("bookmark_invalid", "Cannot read the existing bookmark state.")
        try:
            observed = digest(config.bookmark.read_bytes())
        except OSError:
            raise ReadingError("bookmark_changed", "The bookmark changed during anchoring; retry.") from None
    else:
        previous, previous_documents, observed = {}, {}, None
    if page is None:
        if type(previous.get("page")) is not int:
            raise ReadingError("invalid_page", "Supply the page when initializing the bookmark.")
        page = previous["page"]
        if after is None:
            page_complete = bool(previous.get("page_complete"))
    documents = {}
    for source, end_quote, begin_quote in [
        ("text", after, start),
        ("translation", translation_after, translation_start),
    ]:
        if end_quote is None:
            if source == "translation":
                continue  # Dropped until sync-translation re-anchors it.
            if source not in previous_documents:
                raise ReadingError("start_required",
                                   "Supply the page, last-read quotation and start when initializing.")
            raw, _ = source_bytes(config, source)
            if previous_documents[source].get("sha256") != digest(raw):
                raise ReadingError("boundary_stale", "The source changed; re-anchor before updating.")
            documents[source] = previous_documents[source]
            continue
        old = previous_documents.get(source, {})
        begin_quote = begin_quote or old.get("start_anchor")
        if not begin_quote:
            raise ReadingError("start_required", f"Supply a start quotation for {source}.")
        raw, text = source_bytes(config, source)
        begin, _ = unique_span(text, begin_quote)
        _, end = unique_span(text, end_quote)
        if begin >= end:
            raise ReadingError("invalid_boundary", "The stop quotation must follow the start.")
        documents[source] = {"sha256": digest(raw), "start": begin, "end": end,
                             "start_anchor": begin_quote, "end_anchor": end_quote}
    known = previous.get("translation_sync")
    if not sync_shape(known):
        known = None
    if known is None and "translation" in previous_documents and "text" in previous_documents:
        # Records written before sync tracking: recover the pair from live documents.
        known = {"last_en_end": previous_documents["text"]["end"],
                 "last_zh_end": previous_documents["translation"]["end"]}
    if known is not None:
        if documents["text"]["end"] < known["last_en_end"]:
            raise ReadingError("english_bookmark_rewound",
                               "The original bookmark moved behind the synced translation; repair manually.")
        if "translation" in documents and documents["translation"]["end"] <= known["last_zh_end"]:
            raise ReadingError("translation_rewound",
                               "The new translation stop must follow the last synced position.")
    sync = known
    if "translation" in documents:
        sync = {"last_en_end": documents["text"]["end"],
                "last_zh_end": documents["translation"]["end"]}
    state = {"version": 2, "page": page, "page_complete": bool(page_complete),
             "documents": documents}
    if sync is not None:
        state["translation_sync"] = sync
    # Refuse to overwrite a bookmark changed while anchoring.
    try:
        current = digest(config.bookmark.read_bytes()) if config.bookmark.exists() else None
    except OSError:
        current = "missing"
    if current != observed:
        raise ReadingError("bookmark_changed", "The bookmark changed during anchoring; retry.")
    config.bookmark.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(config.bookmark, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    _project(config, page)
    boundary = load_boundary(config)
    if sync is not None and translation_after is not None:
        remember_pair(config, [documents["text"]["end"], documents["translation"]["end"], page])
    return boundary
