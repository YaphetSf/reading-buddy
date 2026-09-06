"""Build citeable passages from already permitted sources."""

from bisect import bisect_left
from dataclasses import dataclass
import html
import json
import re
from statistics import median

from .boundary import digest, fold


class PageMap:
    """Sort, deduplicate and isotonic-fit noisy anchors. Never a safety boundary."""

    def __init__(self, config, document, bookmark_page):
        pairs = {}
        if config.calibration:
            try:
                data = json.loads((config.root / config.calibration).read_text())["calib"]
                for pos, page in data:
                    if (type(pos) is int and type(page) in (int, float)
                            and document.start < pos < document.end
                            and config.page_range[0] <= page <= bookmark_page):
                        pairs.setdefault(pos, []).append(page)
            except (OSError, ValueError, KeyError, TypeError):
                pairs = {}
        points = [(document.start, float(config.page_range[0]))]
        points += [(p, float(median(values))) for p, values in sorted(pairs.items())]
        points += [(document.end, float(bookmark_page))]
        self.positions = [p for p, _ in points]
        pools = []
        for _, value in points:
            pools.append([value, 1])
            while len(pools) > 1 and pools[-2][0] / pools[-2][1] > pools[-1][0] / pools[-1][1]:
                total, count = pools.pop()
                pools[-1][0] += total
                pools[-1][1] += count
        self.pages = [total / count for total, count in pools for _ in range(count)]
        self.calibrated = bool(pairs)

    def page_at(self, pos):
        if not self.calibrated:
            return None
        i = min(max(bisect_left(self.positions, pos), 1), len(self.positions) - 1)
        a, b = self.positions[i - 1:i + 1]
        ratio = (min(max(pos, a), b) - a) / (b - a)
        return round(self.pages[i - 1] + ratio * (self.pages[i] - self.pages[i - 1]), 1)

    def position_at(self, page):
        if not self.calibrated:
            return None
        i = min(max(bisect_left(self.pages, page), 1), len(self.pages) - 1)
        a, b = self.pages[i - 1:i + 1]
        if a == b:
            return self.positions[i - 1]
        return round(self.positions[i - 1] + (page - a) / (b - a)
                     * (self.positions[i] - self.positions[i - 1]))


@dataclass(frozen=True)
class Passage:
    source: str
    path: str
    revision: str
    start: int
    end: int
    text: str
    page: float | None = None
    url: str | None = None

    @property
    def id(self):
        return f"{self.source}:{self.revision}:{self.start}:{self.end}"


def text_passages(document, page_map=None, size=1600, overlap=300, part_marker=None):
    """Overlapping, word-bounded windows tolerate the ebook's hard-wrapped lines."""
    # Part markers are structural boundaries, not content to index.
    cursor = 0
    regions = []
    if part_marker is not None:
        for marker in part_marker.finditer(document.text):
            if marker.start() > cursor:
                regions.append((cursor, marker.start()))
            cursor = marker.end()
    regions.append((cursor, len(document.text)))
    for lo, hi in regions:
        start = lo
        while start < hi:
            while start < hi and document.text[start].isspace():
                start += 1
            if start >= hi:
                break
            end = min(start + size, hi)
            if end < hi:
                # Prefer a nearby sentence end, then a word boundary.
                segment = document.text[start + size // 2:end]
                breaks = list(re.finditer(r'[.!?][”’"\']?(?=\s)', segment))
                if breaks:
                    end = start + size // 2 + breaks[-1].end()
                else:
                    cut = document.text.rfind(" ", start + size // 2, end)
                    if cut >= 0:
                        end = cut
            while end > start and document.text[end - 1].isspace():
                end -= 1
            absolute = document.start + start
            yield Passage(document.source, document.path, document.revision,
                          absolute, document.start + end, document.text[start:end],
                          page_map.page_at(absolute) if page_map else None)
            if end >= hi or not document.text[end:hi].strip():
                break
            next_start = max(start + 1, end - overlap)
            while next_start < end and not document.text[next_start - 1].isspace():
                next_start += 1
            start = next_start


PAGE_HEADER = re.compile(r"^={2,}\s*(?:Page\s+|pp?\.\s*)?(\d+)\b", re.I)
PAGE_REFS = [
    re.compile(r"\b(?:pp?|pages?)\.?\s*(\d+)(?:\s*[-–—]\s*(\d+))?", re.I),
    re.compile(r"Pages?[_ ]+(\d+)[_ –—-]+(\d+)", re.I),
    re.compile(r"#(?:Page|p)[_ ]?(\d+)", re.I),
]


def clean_wiki(block):
    text = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", block, flags=re.I)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[(?:https?://\S+)\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[https?://[^\]]+\]", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text.replace("'''", "").replace("''", ""))).strip()


def wiki_passages(config, ceiling):
    """Only open complete allowed ranges. Unbounded indexes/errata are excluded."""
    annotations = config.annotations
    if not annotations:
        return
    directory = config.root / annotations["directory"]
    url_base = annotations["url_base"]
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("Pages_*-*.txt")):
        match = re.fullmatch(r"Pages_(\d+)-(\d+)\.txt", path.name)
        if not match:
            continue
        lo, hi = map(int, match.groups())
        if (lo, hi) not in annotations["ranges"] or hi > ceiling:
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            continue
        revision = digest(text.encode("utf-8"))[:20]
        page, start, offset, buf = None, 0, 0, []

        def emit():
            if page is None or not buf:
                return None
            block = "".join(buf)
            # Unexpanded templates have unknown provenance; do not expose them.
            if "{{" in block:
                return None
            refs = [int(n) for pat in PAGE_REFS for m in pat.finditer(block)
                    for n in m.groups() if n]
            if any(n > ceiling for n in refs):
                return None
            cleaned = clean_wiki(block)
            if not cleaned:
                return None
            return Passage("wiki", str(path.relative_to(config.root)), revision, start,
                           start + len(block), cleaned, page,
                           url_base + path.stem + "#Page_" + str(page))

        for line in text.splitlines(keepends=True):
            heading = PAGE_HEADER.match(line)
            term = re.match(r"^(?:<[^>]+>)*\s*'''", line)
            section = line.startswith("=")
            if heading or section or not line.strip() or (term and buf):
                if passage := emit():
                    yield passage
                buf = []
                if section:
                    page = int(heading.group(1)) if heading else None
                    if page is not None and not lo <= page <= min(hi, ceiling):
                        page = None
            if line.strip() and not section:
                if not buf:
                    start = offset
                buf.append(line)
            offset += len(line)
        if passage := emit():
            yield passage
