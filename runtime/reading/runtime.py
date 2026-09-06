"""Stateless JSON requests over a fresh, exact reading snapshot."""

from pathlib import Path
import re
import sqlite3
import unicodedata

from .boundary import ReadingError, digest, fold, load_boundary, normalized_spans
from . import summary as running_summary
from .sources import PageMap, Passage, text_passages, wiki_passages


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ReadingError("invalid_request", f"{name} must be an integer from {low} to {high}.")
    return value


def window(start, end, low, high, size):
    """Center an excerpt on a hit without ever extending past the permitted span."""
    size = min(size, high - low)
    left = max(low, start - max(0, size - (end - start)) // 2)
    left = min(left, high - size)
    return left, min(high, left + size)


def query_spans(text, query):
    """Whole-word edges prevent a name query from centering on a longer word."""
    needle = fold(query)
    def word_character(ch):
        return ch.isdecimal() or any(script in unicodedata.name(ch, "")
                                     for script in ("LATIN", "GREEK", "CYRILLIC"))
    for start, end in normalized_spans(text, query):
        if word_character(needle[0]) and start and word_character(text[start - 1]):
            continue
        if word_character(needle[-1]) and end < len(text) and word_character(text[end]):
            continue
        yield start, end


class _Snapshot:
    def __init__(self, config):
        self.config = config
        self.root = config.root
        self.boundary = load_boundary(config)
        self.documents = self.boundary.documents
        self.pages = PageMap(config, self.documents["text"], self.boundary.page)
        self._wiki = None

    @property
    def wiki(self):
        if self._wiki is None:
            self._wiki = list(wiki_passages(self.config, self.boundary.wiki_ceiling))
        return self._wiki

    def sources(self, source):
        if source not in {"text", "translation", "wiki", "all"}:
            raise ReadingError("invalid_request", "source must be text, translation, wiki, or all.")
        if source == "translation" and source not in self.documents:
            raise ReadingError("translation_unanchored",
                               "Translation needs its own exact start/stop anchors; English page estimates cannot bound it.")
        return ["text", *(["translation"] if "translation" in self.documents else []), "wiki"] if source == "all" else [source]

    def status(self):
        return {"status": "ok", "bookmark": self.boundary.summary(),
                "summary": running_summary.status_info(self.config, self.boundary),
                "sources": {
                    "text": {"available": True, "allowed_characters": len(self.documents["text"].text)},
                    "wiki": {"available": bool(self.wiki), "policy": "complete_ranges_only",
                             "available_ranges": sorted({Path(p.path).stem for p in self.wiki})},
                    "translation": {"available": "translation" in self.documents,
                                    "policy": "independent_exact_anchor"}},
                "page_lookup": "estimated" if self.pages.calibrated else "unavailable",
                "endnotes": "not_enabled"}

    def assert_current(self):
        if digest(self.config.bookmark.read_bytes()) != self.boundary.state_checksum:
            raise ReadingError("bookmark_changed", "The bookmark changed during retrieval; retry.")
        for document in self.documents.values():
            try:
                unchanged = digest((self.root / document.path).read_bytes()) == document.checksum
            except OSError:
                unchanged = False
            if not unchanged:
                raise ReadingError("source_changed", "A source changed during retrieval; re-anchor and retry.")

    def text_passage(self, source, start, end):
        document = self.documents[source]
        start = max(document.start, start)
        end = min(document.end, end)
        return Passage(source, document.path, document.revision, start, end,
                       document.text[start - document.start:end - document.start],
                       self.pages.page_at(start) if source == "text" else None)

    def result(self, passage, match=None, size=1200):
        original = passage
        if passage.source in self.documents:
            if match is None:
                match = (passage.start, passage.start)
            lo, hi = window(*match, passage.start, passage.end, size)
            passage = self.text_passage(passage.source, lo, hi)
            displayed = re.sub(r"\s+", " ", passage.text).strip()
            if self.config.part_marker_source:
                displayed = re.sub(self.config.part_marker_source, "", displayed)
        else:
            # Wiki text is rendered from a raw block. The reference identifies that
            # block; rendered character positions must not be presented as raw offsets.
            if match is None:
                lo = 0
            else:
                lo, _ = window(*match, 0, len(passage.text), size)
            displayed = passage.text[lo:lo + size]
        citation = {"path": passage.path, "chars": [passage.start, passage.end]}
        if passage.source == "text":
            citation["page_estimate"] = passage.page
        elif passage.source == "wiki":
            citation.update(annotation_page=passage.page, url=passage.url,
                            offsets_refer_to="raw_wikitext_block")
        return {"id": passage.id, "source": passage.source, "text": displayed,
                "citation": citation,
                "excerpt_truncated": len(displayed) < len(re.sub(r"\s+", " ", original.text).strip())}

    def passages(self, sources):
        for source in sources:
            if source == "wiki":
                yield from self.wiki
            else:
                yield from text_passages(self.documents[source],
                                         self.pages if source == "text" else None,
                                         part_marker=self.config.part_marker)

    def search(self, query, source="all", mode="all", order="relevance", limit=6, max_chars=6000):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise ReadingError("invalid_request", "query must contain 1–500 characters.")
        if mode not in {"all", "any", "phrase"} or order not in {"relevance", "reading", "recent"}:
            raise ReadingError("invalid_request", "Invalid search mode or order.")
        limit = integer(limit, "limit", 1, 20)
        budget = integer(max_chars, "max_chars", 64, 12000)
        sources = self.sources(source)
        candidates = []
        if mode == "phrase":
            # Search the allowed document, so a phrase crossing a chunk boundary is
            # still found. Index chunking is never a correctness boundary.
            for src in sources:
                if src == "wiki":
                    for passage in self.wiki:
                        for lo, hi in query_spans(passage.text, query):
                            candidates.append((passage, (lo, hi), (0, 0.0)))
                else:
                    doc = self.documents[src]
                    for lo, hi in query_spans(doc.text, query):
                        hit = (doc.start + lo, doc.start + hi)
                        start, end = window(*hit, doc.start, doc.end, 1600)
                        candidates.append((self.text_passage(src, start, end), hit, (0, 0.0)))
        else:
            terms = list(dict.fromkeys(re.findall(r"[^\W_]+", fold(query))))
            if not terms or len(terms) > 32:
                raise ReadingError("invalid_request", "Use 1–32 searchable words.")
            passages = list(self.passages(sources))
            connection = sqlite3.connect(":memory:")
            try:
                # This index contains only allowed text, including its term statistics.
                connection.execute("CREATE VIRTUAL TABLE evidence USING fts5(text, tokenize='unicode61 remove_diacritics 0')")
                connection.executemany("INSERT INTO evidence(rowid,text) VALUES (?,?)",
                                       ((i + 1, fold(p.text)) for i, p in enumerate(passages)))
                joiner = " AND " if mode == "all" else " OR "
                expression = joiner.join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = connection.execute(
                    "SELECT rowid,bm25(evidence) FROM evidence WHERE evidence MATCH ? ORDER BY bm25(evidence),rowid",
                    (expression,)).fetchall()
            except sqlite3.Error:
                raise ReadingError("search_unavailable", "SQLite with FTS5 support is required.") from None
            finally:
                connection.close()
            for rowid, score in rows:
                passage = passages[rowid - 1]
                exact = next(query_spans(passage.text, query), None)
                if exact:
                    local = exact
                else:
                    local = next((m for term in terms
                                  if (m := next(query_spans(passage.text, term), None))), None)
                if local is None:
                    continue
                hit = local if passage.source == "wiki" else tuple(passage.start + x for x in local)
                candidates.append((passage, hit, (0 if exact else 1, score)))
        if order == "relevance":
            candidates.sort(key=lambda x: (x[2], x[0].source, x[0].start))
        else:
            # Sources have independent positions; source grouping is explicit.
            candidates.sort(key=lambda x: (sources.index(x[0].source),
                                           (x[0].page or 0) * (-1 if order == "recent" else 1)
                                           if x[0].source == "wiki" else 0,
                                           (x[0].start + x[1][0] if x[0].source == "wiki" else x[1][0])
                                           * (-1 if order == "recent" else 1)))
        results, seen = [], []
        remaining = budget
        size = min(1600, max(64, budget // max(1, min(limit, len(candidates)))))
        for passage, hit, _ in candidates:
            if len(results) >= limit or remaining < 1:
                break
            item = self.result(passage, hit, min(size, remaining))
            lo, hi = item["citation"]["chars"]
            duplicate = any(src == passage.source and path == passage.path
                            and max(0, min(hi, b) - max(lo, a)) >= .5 * min(hi - lo, b - a)
                            for src, path, a, b in seen)
            if duplicate:
                continue
            seen.append((passage.source, passage.path, lo, hi))
            results.append(item)
            remaining -= len(item["text"])
        return {"status": "ok" if results else "no_matches",
                "bookmark": self.boundary.summary(),
                "query": query, "scope": sources, "mode": mode, "order": order,
                "results": results, "returned_characters": budget - remaining,
                "max_chars": budget}

    def read(self, reference, before=0, after=0, max_chars=6000):
        before = integer(before, "before", 0, 6000)
        after = integer(after, "after", 0, 6000)
        budget = integer(max_chars, "max_chars", 64, 12000)
        if not isinstance(reference, str):
            raise ReadingError("invalid_reference", "Supply an id returned by retrieval.")
        parts = reference.split(":")
        if len(parts) != 4:
            raise ReadingError("invalid_reference", "Supply an id returned by retrieval.")
        source, revision, a, b = parts
        try:
            start, end = int(a), int(b)
        except ValueError:
            raise ReadingError("invalid_reference", "Invalid character offsets.") from None
        if source == "wiki":
            passage = next((p for p in self.wiki if p.id == reference), None)
            if passage is None:
                raise ReadingError("invalid_reference", "Reference is unavailable in the current reading scope.")
            if before or after:
                raise ReadingError("invalid_request", "Wiki references expand only within their annotation block.")
        else:
            document = self.documents.get(source)
            if (document is None or revision != document.revision
                    or not document.start <= start < end <= document.end):
                raise ReadingError("invalid_reference", "Reference is unavailable in the current reading scope.")
            passage = self.text_passage(source, start - before, end + after)
        item = self.result(passage, (start, end) if source != "wiki" else None, budget)
        return {"status": "ok", "bookmark": self.boundary.summary(), "results": [item],
                "returned_characters": len(item["text"]), "max_chars": budget}

    def current(self, source="text", max_chars=2500):
        budget = integer(max_chars, "max_chars", 64, 12000)
        if source not in {"text", "translation"}:
            raise ReadingError("invalid_request", "current supports text or translation.")
        self.sources(source)
        doc = self.documents[source]
        passage = self.text_passage(source, doc.end - budget, doc.end)
        item = self.result(passage, (passage.end, passage.end), budget)
        return {"status": "ok", "bookmark": self.boundary.summary(), "results": [item],
                "returned_characters": len(item["text"]), "max_chars": budget}

    def page(self, page, max_chars=2500):
        integer(page, "page", *self.config.page_range)
        budget = integer(max_chars, "max_chars", 64, 12000)
        if page > self.boundary.page:
            raise ReadingError("outside_bookmark", "Requested page is beyond the current bookmark.")
        pos = self.pages.position_at(page)
        if pos is None:
            raise ReadingError("page_map_unavailable", "Use an original-text quotation; no calibrated page map is available.")
        doc = self.documents["text"]
        start, end = window(pos, pos, doc.start, doc.end, budget)
        item = self.result(self.text_passage("text", start, end), (pos, pos), budget)
        return {"status": "ok", "bookmark": self.boundary.summary(), "requested_page": page,
                "location_is_estimated": True, "results": [item],
                "returned_characters": len(item["text"]), "max_chars": budget}

    def range(self, from_offset=0, to_offset=0, max_chars=12000):
        """In-order reading of the allowed slice, for catch-up and compression re-reads."""
        document = self.documents["text"]
        start = integer(from_offset, "from", 0, 2 ** 31)
        end = integer(to_offset, "to", 1, 2 ** 31)
        if start >= end:
            raise ReadingError("invalid_request", "from must be below to.")
        budget = integer(max_chars, "max_chars", 64, 12000)
        if start >= document.end:
            raise ReadingError("outside_bookmark", "Nothing to read past the bookmark; `current` shows the tail.")
        lo = max(document.start, start)
        hi = min(document.end, end, lo + budget)
        text = document.text[lo - document.start:hi - document.start]
        if self.config.part_marker_source:
            text = re.sub(self.config.part_marker_source, "", text)
        return {"status": "ok", "bookmark": self.boundary.summary(),
                "citation": {"path": document.path, "chars": [lo, hi]},
                "page_estimate": self.pages.page_at(lo),
                "allowed_end": document.end, "truncated": hi < end,
                "text": text, "returned_characters": len(text), "max_chars": budget}

    def summary(self, markdown=False):
        if not isinstance(markdown, bool):
            raise ReadingError("invalid_request", "markdown must be a boolean.")
        return running_summary.read_summary(self.config, markdown=markdown)


class ReadingRuntime:
    """One JSON object in/out. Every request revalidates the on-disk bookmark."""

    ARGUMENTS = {
        "status": set(),
        "search": {"query", "source", "mode", "order", "limit", "max_chars"},
        "read": {"reference", "before", "after", "max_chars"},
        "current": {"source", "max_chars"},
        "page": {"page", "max_chars"},
        "range": {"from", "to", "max_chars"},
        "summary": {"markdown"},
    }

    def __init__(self, config):
        self.config = config

    def request(self, request):
        try:
            if not isinstance(request, dict):
                raise ReadingError("invalid_request", "Request must be a JSON object.")
            action = request.get("action")
            if not isinstance(action, str) or action not in self.ARGUMENTS:
                raise ReadingError("invalid_request", "Unknown retrieval action.")
            kwargs = {k: v for k, v in request.items() if k != "action"}
            if set(kwargs) - self.ARGUMENTS[action]:
                raise ReadingError("invalid_request", "Unknown request parameters.")
            for key in {"source", "mode", "order"} & set(kwargs):
                if not isinstance(kwargs[key], str):
                    raise ReadingError("invalid_request", f"{key} must be a string.")
            required = {"search": {"query"}, "read": {"reference"}, "page": {"page"},
                        "range": {"from", "to"}}.get(action, set())
            missing = sorted(required - set(kwargs))
            if missing:
                raise ReadingError("invalid_request", f"Missing {missing[0]}.")
            if action == "range":
                kwargs["from_offset"] = kwargs.pop("from")
                kwargs["to_offset"] = kwargs.pop("to")
            snapshot = _Snapshot(self.config)
            response = getattr(snapshot, action)(**kwargs)
            snapshot.assert_current()
        except ReadingError as exc:
            response = {"status": "blocked", "error": {"code": exc.code, "message": str(exc)}}
        return {"schema_version": 1, **response}
