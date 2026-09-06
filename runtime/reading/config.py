"""Book configuration: every book-specific fact lives in .reading/book.json."""

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .boundary import ReadingError


HOME = ".reading"  # the runtime's conventional folder inside a book workspace


@dataclass(frozen=True)
class BookConfig:
    root: Path
    title: str
    documents: dict          # source name -> workspace-relative path; "text" is primary
    page_range: tuple        # inclusive (low, high) of main-text pages
    bookmark: Path           # .reading/bookmark.json; the single reading truth
    note: str | None         # projection target; written, never read
    calibration: str | None  # advisory char-offset ↔ page anchors
    alignment: str | None    # advisory English ↔ translation offset pairs
    part_marker: re.Pattern | None   # structural section markers, not content
    part_marker_source: str | None
    annotations: dict | None  # {"directory", "ranges", "url_base"}; absent = none


def _relative(root, value):
    """Reject paths that escape the book workspace."""
    try:
        candidate = (root / value).resolve()
    except OSError:
        return False
    base = root.resolve()
    return candidate.is_relative_to(base) and candidate != base


def load_config(root):
    root = Path(root)
    try:
        state = json.loads((root / HOME / "book.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReadingError("config_missing",
                           "A .reading/book.json descriptor is required in the workspace.") from None
    try:
        documents = state["documents"]
        page_range = state["page_range"]
        if (state.get("version") != 1 or not isinstance(state.get("title"), str)
                or not state["title"].strip()
                or not isinstance(documents, dict) or "text" not in documents
                or not all(isinstance(path, str) and path for path in documents.values())
                or not isinstance(page_range, list) or len(page_range) != 2
                or any(type(edge) is not int for edge in page_range)
                or page_range[0] >= page_range[1]
                or any(state.get(key) is not None and not isinstance(state[key], str)
                       for key in ("note", "calibration", "alignment", "part_marker"))):
            raise ValueError
        for path in list(documents.values()) + [state.get(key) for key in
                                                ("note", "calibration", "alignment")]:
            if path and not _relative(root, path):
                raise ValueError
        part_source = state.get("part_marker")
        part_marker = re.compile("(?m)^" + part_source + "[ \t]*\r?\n?") if part_source else None
        annotations = state.get("annotations")
        if annotations is not None:
            ranges = annotations["ranges"]
            if (not isinstance(annotations.get("directory"), str)
                    or not isinstance(annotations.get("url_base"), str)
                    or not isinstance(ranges, list)
                    or not all(isinstance(pair, list) and len(pair) == 2
                               and all(type(edge) is int for edge in pair)
                               and pair[0] <= pair[1] for pair in ranges)
                    or not _relative(root, annotations["directory"])):
                raise ValueError
            annotations = {"directory": annotations["directory"],
                           "url_base": annotations["url_base"],
                           "ranges": {(pair[0], pair[1]) for pair in ranges}}
    except (KeyError, TypeError, ValueError, re.error):
        raise ReadingError("config_invalid",
                           "book.json is not a valid book descriptor.") from None
    return BookConfig(root, state["title"], dict(documents), tuple(page_range),
                      root / HOME / "bookmark.json",
                      state.get("note"), state.get("calibration"), state.get("alignment"),
                      part_marker, part_source, annotations)
