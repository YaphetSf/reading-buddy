"""Runtime-owned running summary of already-read text.

summary.json is model-authored content under runtime custody. The runtime owns
every coordinate: entries tile the original text contiguously from the first
body character, each append stamp is derived from the live boundary, and pages
are read off the calibration — the agent only ever supplies prose. Entries
whose span reaches past the current boundary are hidden, not deleted, so a
bookmark rollback needs no cleanup; a changed source marks the whole file
stale. Entry text is commentary with coordinates, never narrative fact and
never itself a retrieval boundary.
"""

import json
import re

from .boundary import ReadingError, _atomic_write, digest, load_boundary, source_bytes
from .config import HOME
from .sources import PageMap

SCHEMA_VERSION = 1
TILE = 12000            # original-text characters one appended entry may cover
MIN_TEXT = 16
MAX_APPEND_TEXT = 4000
MAX_REPLACE_TEXT = 8000
LEVELS = (0, 1, 2)


def _path(config):
    return config.root / HOME / "summary.json"


def _read_state(config):
    """(raw bytes, parsed state); state is None before the first entry."""
    try:
        raw = _path(config).read_bytes()
    except OSError:
        return None, None
    try:
        state = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        raise ReadingError("summary_invalid", "summary.json is unreadable; repair or delete it.") from None
    if (not isinstance(state, dict) or state.get("version") != SCHEMA_VERSION
            or not isinstance(state.get("source_sha256"), str)
            or not isinstance(state.get("entries"), list)):
        raise ReadingError("summary_invalid", "summary.json has an unknown shape; repair or delete it.")
    return raw, state


def _validated_entries(state):
    previous_end = None
    for entry in state["entries"]:
        if (not isinstance(entry, dict)
                or type(entry.get("source_start")) is not int
                or type(entry.get("source_end")) is not int
                or not 0 <= entry["source_start"] < entry["source_end"]
                or not isinstance(entry.get("text"), str) or not entry["text"].strip()
                or type(entry.get("level")) is not int or entry["level"] not in LEVELS
                or entry.get("pages") is not None
                and (not isinstance(entry["pages"], list) or len(entry["pages"]) != 2)):
            raise ReadingError("summary_invalid", "summary.json contains a malformed entry.")
        if previous_end is not None and entry["source_start"] != previous_end:
            raise ReadingError("summary_invalid", "summary.json entries do not tile the text contiguously.")
        previous_end = entry["source_end"]
    return state["entries"]


def _check_source(config, state):
    raw, _ = source_bytes(config, "text")
    if state["source_sha256"] != digest(raw):
        raise ReadingError("summary_stale",
                           "The text source changed after the summary was written; rebuild it.")


def _visible(entries, document):
    return [entry for entry in entries
            if entry["source_start"] >= document.start and entry["source_end"] <= document.end]


def _pages(config, boundary, start, end):
    page_map = PageMap(config, boundary.documents["text"], boundary.page)
    pages = [page_map.page_at(start), page_map.page_at(end)]
    if all(page is None for page in pages):
        return None
    return [None if page is None else int(round(page)) for page in pages]


def _suggest_merge(items, overage):
    """Oldest contiguous run of level-0 entries worth merging first."""
    run, chars = [], 0
    for item in items:
        if item["level"] != 0:
            break
        run.append(item["index"])
        chars += len(item["text"])
        if len(run) >= 2 and chars >= overage:
            return [run[0], run[-1]]
    return [run[0], run[-1]] if len(run) >= 2 else None


def _payload(config, boundary):
    """Visible entries and their accounting: the one shape the summary is reported in."""
    _, state = _read_state(config)
    if state is None:
        return {"entries": [], "hidden": 0, "chars": 0,
                "budget": config.summary_budget, "over_budget": False}
    _check_source(config, state)
    entries = _validated_entries(state)
    visible = _visible(entries, boundary.documents["text"])
    chars = sum(len(entry["text"]) for entry in visible)
    payload = {"entries": [{"index": number,
                            "chars": [entry["source_start"], entry["source_end"]],
                            "pages": entry["pages"], "level": entry["level"],
                            "text": entry["text"]}
                           for number, entry in enumerate(visible, 1)],
               "hidden": len(entries) - len(visible), "chars": chars,
               "budget": config.summary_budget, "over_budget": chars > config.summary_budget}
    if payload["over_budget"]:
        suggestion = _suggest_merge(payload["entries"], chars - config.summary_budget)
        if suggestion:
            payload["suggest_merge"] = suggestion
    return payload


def read_summary(config, markdown=False):
    boundary = load_boundary(config)
    payload = _payload(config, boundary)
    if markdown:
        payload["markdown"] = _markdown(config, boundary, payload["entries"])
    return {"status": "ok", "bookmark": boundary.summary(), **payload}


def status_info(config, boundary):
    """Status carries the entries themselves; recalling them is not a second command."""
    try:
        payload = _payload(config, boundary)
    except ReadingError as exc:
        return {"available": False, "reason": exc.code, "entries": []}
    if not payload["entries"]:
        return {"available": False, "reason": "empty", **payload}
    return {"available": True, **payload}


def _markdown(config, boundary, items):
    lines = [f"# Summary — {config.title}", "",
             "Runtime-owned recap of already-read text; entries are claims with coordinates.",
             f"Bounded at the bookmark ({boundary.summary()['boundary']})."]
    for item in items:
        pages = item["pages"]
        where = (f"pp. {pages[0]}–{pages[1]}" if pages
                 else f"chars {item['chars'][0]}–{item['chars'][1]}")
        lines += ["", f"## {item['index']}. {where} · level {item['level']}", "", item["text"]]
    return "\n".join(lines)


def append_summary(config, text):
    """Stamp the next un-summarized tile as one level-0 entry; the agent supplies only prose."""
    if not isinstance(text, str) or not MIN_TEXT <= len(text.strip()) <= MAX_APPEND_TEXT:
        raise ReadingError("invalid_request",
                           f"Summary text must be {MIN_TEXT}–{MAX_APPEND_TEXT} characters.")
    boundary = load_boundary(config)
    document = boundary.documents["text"]
    _, state = _read_state(config)
    if state is None:
        raw, _ = source_bytes(config, "text")
        state = {"version": SCHEMA_VERSION, "source_sha256": digest(raw), "entries": []}
    else:
        _check_source(config, state)
    entries = _validated_entries(state)
    cursor = max([document.start] + [entry["source_end"] for entry in entries])
    if cursor > document.end:
        raise ReadingError("summary_caught_up",
                           "The summary extends past the bookmark; entries resurface as reading advances.")
    if cursor == document.end:
        raise ReadingError("summary_caught_up",
                           "The summary already covers the bookmark; report new reading progress first.")
    tile_end = min(cursor + TILE, document.end)
    entry = {"source_start": cursor, "source_end": tile_end,
             "pages": _pages(config, boundary, cursor, tile_end), "level": 0, "text": text.strip()}
    state["entries"] = entries + [entry]
    _atomic_write(_path(config), json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return {"status": "ok",
            "appended": {"chars": [cursor, tile_end], "pages": entry["pages"], "level": 0},
            "caught_up": tile_end >= document.end, "bookmark": boundary.summary()}


def replace_summary(config, span, level, text):
    """Merge visible entries I–J into one coarser entry after backing up the file."""
    if not isinstance(span, str) or not re.fullmatch(r"\d+\s*-\s*\d+", span.strip()):
        raise ReadingError("invalid_request", "Use --replace I-J with visible entry indices, I ≤ J.")
    low, high = (int(part) for part in re.split(r"\s*-\s*", span.strip()))
    if low > high:
        raise ReadingError("invalid_request", "Use --replace I-J with I ≤ J.")
    if type(level) is not int or level not in LEVELS:
        raise ReadingError("invalid_request", "--level must be 0, 1 or 2.")
    if not isinstance(text, str) or not MIN_TEXT <= len(text.strip()) <= MAX_REPLACE_TEXT:
        raise ReadingError("invalid_request",
                           f"Replacement text must be {MIN_TEXT}–{MAX_REPLACE_TEXT} characters.")
    boundary = load_boundary(config)
    document = boundary.documents["text"]
    raw, state = _read_state(config)
    if state is None:
        raise ReadingError("summary_missing", "No summary exists yet; append entries first.")
    _check_source(config, state)
    entries = _validated_entries(state)
    positions = [index for index, entry in enumerate(entries)
                 if entry["source_start"] >= document.start and entry["source_end"] <= document.end]
    if not positions:
        raise ReadingError("invalid_request", "No visible entries to replace.")
    if not 1 <= low <= high <= len(positions):
        raise ReadingError("invalid_request",
                           f"--replace takes indices 1–{len(positions)} of the visible entries.")
    first, last = positions[low - 1], positions[high - 1]
    merged = {"source_start": entries[first]["source_start"],
              "source_end": entries[last]["source_end"],
              "pages": _pages(config, boundary, entries[first]["source_start"],
                              entries[last]["source_end"]),
              "level": level, "text": text.strip()}
    _atomic_write(_path(config).with_name("summary.json.bak"), raw.decode("utf-8"))
    state["entries"] = entries[:first] + [merged] + entries[last + 1:]
    _atomic_write(_path(config), json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return {"status": "ok", "replaced": [low, high],
            "entry": {"chars": [merged["source_start"], merged["source_end"]],
                      "pages": merged["pages"], "level": level, "text": merged["text"]},
            "backup": "summary.json.bak", "bookmark": boundary.summary()}
