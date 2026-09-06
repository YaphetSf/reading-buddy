"""Bounded probes that locate translation positions without opening retrieval."""

import json
import re

from .boundary import ReadingError, load_boundary, normalized_spans, source_bytes


WINDOW = 2200            # Characters per probe window.
MAX_WINDOWS = 3          # Windows per probe call.
BACK = 3000              # Characters a window may reach before its estimate.
BOOTSTRAP_SLACK = 40000  # Forward band while no anchor pair exists yet.
SYNC_SLACK = 6000        # Minimum forward band once anchor pairs exist.


def alignment_pairs(config):
    """Advisory (English offset, translation offset, page) history, deduplicated."""
    if not config.alignment:
        return []
    try:
        pairs = json.loads((config.root / config.alignment).read_text(encoding="utf-8"))["pairs"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(pairs, list):
        return []
    latest = {}
    for pair in pairs:
        if (isinstance(pair, list) and len(pair) == 3
                and all(type(value) is int and value >= 0 for value in pair)):
            latest[pair[0]] = pair
    return [latest[en] for en in sorted(latest)]


def _regions(text, part_marker):
    """Ordered content regions; structural part markers are not content."""
    if part_marker is None:
        return [(0, len(text))]
    bounds = [0] + [marker.start() for marker in part_marker.finditer(text)] + [len(text)]
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def _fraction(regions, position):
    total = sum(b - a for a, b in regions) or 1
    seen = 0
    for a, b in regions:
        if position <= b:
            return min(1.0, (seen + min(max(position - a, 0), b - a)) / total)
        seen += b - a
    return 1.0


def _position(regions, fraction):
    total = sum(b - a for a, b in regions) or 1
    remaining = fraction * total
    seen = 0
    for a, b in regions:
        if seen + (b - a) >= remaining:
            return a + (remaining - seen)
        seen += b - a
    return float(regions[-1][1])


def _estimate(config, en_text, zh_text, en_target):
    """Estimate the translation offset for an English offset, with band slack."""
    pairs = alignment_pairs(config)
    en_regions = _regions(en_text, config.part_marker)
    zh_regions = _regions(zh_text, config.part_marker)
    if pairs and en_target >= pairs[0][0]:
        earlier = [pair for pair in pairs if pair[0] <= en_target]
        a_en, a_zh = earlier[-1][0], earlier[-1][1]
        later = [pair for pair in pairs if pair[0] > en_target]
        if later:
            b_en, b_zh = later[0][0], later[0][1]
            estimate = a_zh + (en_target - a_en) * (b_zh - a_zh) / (b_en - a_en)
        elif len(earlier) >= 2:
            slope = (a_zh - earlier[-2][1]) / max(1, a_en - earlier[-2][0])
            estimate = a_zh + (en_target - a_en) * min(slope, 1.0)
        else:
            estimate = a_zh + (en_target - a_en) * (len(zh_text) - a_zh) / max(1, len(en_text) - a_en)
        estimate = min(max(estimate, a_zh), len(zh_text))
        slack = max(SYNC_SLACK, int(0.53 * max(0, en_target - pairs[-1][0])))
        return estimate, slack, pairs
    # Bootstrap: blend overall length ratio with order-matched part structure.
    ratio = len(zh_text) / max(1, len(en_text))
    structural = _position(zh_regions, _fraction(en_regions, en_target))
    return 0.5 * en_target * ratio + 0.5 * structural, BOOTSTRAP_SLACK, pairs


def _display(text, part_marker_source=None):
    body = re.sub(part_marker_source, "", text) if part_marker_source else text
    return re.sub(r"\s+", " ", body).strip()


def probe(config, target="stop", shift=0, near=None):
    """Return bounded translation windows around the estimated anchor position.

    The search band is derived only from English reading progress; windows never
    reach beyond it, and probe output is not expandable through read.
    """
    boundary = load_boundary(config)
    if target not in {"start", "stop"}:
        raise ReadingError("invalid_request", "Probe target must be start or stop.")
    if type(shift) is not int:
        raise ReadingError("invalid_request", "shift must be an integer.")
    if "translation" not in config.documents:
        raise ReadingError("invalid_request", "This book declares no translation source.")
    _, en_text = source_bytes(config, "text")
    _, zh_text = source_bytes(config, "translation")
    document = boundary.documents["text"]
    en_target = document.start if target == "start" else document.end
    estimate, slack, pairs = _estimate(config, en_text, zh_text, en_target)
    if target == "start":
        slack = BOOTSTRAP_SLACK  # Start anchoring is a one-time bootstrap.
    floor = max(0, int(estimate) - BACK)
    ceiling = min(len(zh_text), int(estimate) + slack)
    start = floor + shift
    if not floor <= start < ceiling:
        raise ReadingError("window_out_of_range",
                           "The probe must stay inside the estimated search band.")
    spans = []
    if near is not None:
        if not isinstance(near, str) or not 2 <= len(near.strip()) <= 100:
            raise ReadingError("invalid_request", "Use a --near phrase of 2-100 characters.")
        for lo, hi in normalized_spans(zh_text[start:ceiling], near):
            spans.append((start + lo, start + hi))
            if len(spans) >= MAX_WINDOWS:
                break
        if not spans:
            # Do not reveal whether the phrase occurs beyond the band.
            raise ReadingError("probe_no_match", "The phrase does not occur inside the search band.")
    else:
        spans = [(start, start)]
    windows = []
    for lo, hi in spans:
        center = (lo + hi) // 2
        a = max(start, center - WINDOW // 2)
        b = min(ceiling, a + WINDOW)
        a = max(start, b - WINDOW)
        windows.append({"chars": [a, b],
                        "text": _display(zh_text[a:b], config.part_marker_source)})
    return {"bookmark": boundary.summary(),
            "probe": {"target": target,
                      "en_offset": en_target,
                      "zh_estimate": int(estimate),
                      "zh_search_band": [floor, ceiling],
                      "zh_window_start": start,
                      "alignment_pairs": len(pairs),
                      "windows": windows},
            "policy": "Probe windows are bounded by English reading progress; "
                      "verify the passage before anchoring."}
