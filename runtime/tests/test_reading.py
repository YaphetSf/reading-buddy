"""Regression tests use invented text. No unread book content enters test output."""

import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reading.boundary import ReadingError, load_boundary, normalized_spans, set_bookmark
from reading.config import load_config
from reading import summary as summary_module
from reading.runtime import ReadingRuntime, _Snapshot
from reading.sources import PageMap, wiki_passages
from reading.summary import append_summary, replace_summary
from reading.sync import alignment_pairs, probe


START = "An invented reading begins here."
STOP = "The reader stops after this sentence."
UNREAD = " UNREAD_SENTINEL A secret cobalt revelation occurs here. " * 10

ZH_START = "译文的起点句子在这里。"
ZH_EARLY = "唯一的早期句子乙。"
ZH_PHRASE = "那位考生用地图与钴蓝色的指南针确认二零二四年的路线。"
ZH_STOP = "读者在这一句话之后停下来休息。"
ZH_NEXT = "唯一的安全填充句子甲。"
ZH_AHEAD = "之后还有很长的安全填充内容没有情节。"


def write_book(root):
    (root / ".reading").mkdir(exist_ok=True)
    (root / ".reading" / "book.json").write_text(json.dumps({
        "version": 1, "title": "Test",
        "note": "Notes.md",
        "documents": {"text": "source/book_en.txt", "translation": "source/book_zh.txt"},
        "page_range": [1, 100],
        "calibration": "source/pages.json",
        "alignment": "source/align.json",
        "annotations": {"directory": "source/wiki", "url_base": "https://example.test/",
                        "ranges": [[3, 27], [27, 63], [87, 127]]},
    }))


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "source/wiki").mkdir(parents=True)
        write_book(self.root)
        self.note = self.root / "Notes.md"
        self.note.write_text("# Reading\n\n**Current position: p. 50 — updated 2026-09-05**\n")
        self.body = START + "\n" + ("A traveler studies maps and a cobalt compass. " * 80) + STOP
        self.write_source(self.body + UNREAD)
        self.config = load_config(self.root)
        self.bookmark = self.config.bookmark
        self.anchor()
        self.reader = ReadingRuntime(self.config)

    def write_source(self, text):
        (self.root / "source/book_en.txt").write_text(text, encoding="utf-8")
        size = len(self.body)
        # Deliberately unordered, duplicated and non-monotone anchors.
        anchors = [[size - 100, 49], [100, 4], [500, 12], [100, 6],
                   [400, 14], [700, 10], [size + 100, 51]]
        (self.root / "source/pages.json").write_text(json.dumps({"calib": anchors}))

    def anchor(self, page=50, complete=False, **kwargs):
        return set_bookmark(self.config, page, STOP, start=START,
                            page_complete=complete, **kwargs)

    def request(self, action, **kwargs):
        result = self.reader.request({"action": action, **kwargs})
        self.assertNotIn("UNREAD_SENTINEL", json.dumps(result))
        return result

    def test_missing_boundary_fails_closed(self):
        (self.bookmark).unlink()
        self.assertEqual(self.request("search", query="cobalt")["error"]["code"], "boundary_missing")

    def test_stale_source_fails_closed(self):
        with (self.root / "source/book_en.txt").open("a") as f:
            f.write("unread change")
        self.assertEqual(self.request("current")["error"]["code"], "boundary_stale")

    def test_note_is_a_projection_never_read(self):
        self.assertIn("**Current position: p. 50", self.note.read_text())
        self.note.write_text("# rewritten freely in the editor")
        self.assertEqual(self.request("status")["status"], "ok")
        set_bookmark(self.config, 51, STOP)
        self.assertIn("**Current position: p. 51", self.note.read_text())

    def test_snapshot_is_reloaded_for_each_request(self):
        self.assertEqual(self.request("status")["status"], "ok")
        state = json.loads((self.bookmark).read_text())
        state["page"] = 49
        (self.bookmark).write_text(json.dumps(state))
        self.assertEqual(self.request("status")["bookmark"]["page"], 49)

    def test_bookmark_rewrite_during_request_discards_results(self):
        original = _Snapshot.search
        def changing_search(snapshot, **kwargs):
            result = original(snapshot, **kwargs)
            state = json.loads((self.bookmark).read_text())
            state["page"] = 49
            (self.bookmark).write_text(json.dumps(state))
            return result
        with patch.object(_Snapshot, "search", changing_search):
            result = self.request("search", query="cobalt")
        self.assertEqual(result["error"]["code"], "bookmark_changed")
        self.assertNotIn("results", result)

    def test_search_and_expansion_stop_exactly_at_bookmark(self):
        result = self.request("search", query=STOP, mode="phrase", source="text")
        hit = result["results"][0]
        expanded = self.request("read", reference=hit["id"], before=6000, after=6000)
        self.assertTrue(expanded["results"][0]["text"].endswith(STOP))
        self.assertEqual(expanded["results"][0]["citation"]["chars"][1], len(self.body))

    def test_mid_sentence_boundary(self):
        stop = "A traveler studies maps"
        self.body = START + "\n" + stop
        self.write_source(self.body + " UNREAD_SENTINEL and then walks away.")
        set_bookmark(self.config, 50, stop, start=START)
        self.assertTrue(self.request("current")["results"][0]["text"].endswith(stop))
        self.assertEqual(self.request("search", query="walks")["status"], "no_matches")

    def test_unread_match_is_indistinguishable_from_no_match(self):
        result = self.request("search", query="revelation")
        self.assertEqual(result["status"], "no_matches")
        self.assertEqual(result["results"], [])
        self.assertNotIn("suppressed", json.dumps(result))
        self.assertNotIn("beyond", json.dumps(result))

    def test_unread_suffix_cannot_change_content_counts_or_ranking(self):
        first = self.request("search", query="cobalt", max_chars=700)
        self.write_source(self.body + ("different cobalt cobalt future words " * 500))
        self.anchor()
        second = self.request("search", query="cobalt", max_chars=700)
        self.assertEqual(first, second)

    def test_read_rejects_forged_or_stale_references(self):
        rev = self.request("status")["bookmark"]["revision"]
        for ref in [f"text:{rev}:0:{len(self.body) + 1}", f"text:{rev}:-1:10",
                    f"text:{rev}:20:10", "text:old:0:10", "wiki:unknown:0:10",
                    "../../source/book_en.txt"]:
            with self.subTest(ref=ref):
                self.assertEqual(self.request("read", reference=ref)["status"], "blocked")

    def test_phrase_normalization_and_original_offsets(self):
        text = "A ﬁne reader’s\n   compass."
        spans = list(normalized_spans(text, "FINE READER'S compass"))
        self.assertEqual(len(spans), 1)
        a, b = spans[0]
        self.assertEqual(text[a:b], "ﬁne reader’s\n   compass")
        decomposed = "The cafe\u0301 opens."
        a, b = next(normalized_spans(decomposed, "café"))
        self.assertEqual(decomposed[a:b], "cafe\u0301")

    def test_phrase_crosses_index_chunk_boundary(self):
        middle = "A uniquely placed quotation " + "includes many careful words " * 12 + "and ends here"
        self.body = START + " a" * 610 + " " + middle + " b" * 900 + STOP
        self.write_source(self.body + UNREAD)
        self.anchor()
        result = self.request("search", query=middle, mode="phrase", source="text")
        self.assertEqual(result["status"], "ok")
        self.assertIn(middle, result["results"][0]["text"])

    def test_search_excerpt_is_centered_on_match(self):
        word = "distinctivecompass"
        self.body = START + " filler" * 100 + " " + word + " filler" * 100 + STOP
        self.write_source(self.body + UNREAD)
        self.anchor()
        result = self.request("search", query=word, source="text", max_chars=100)
        self.assertIn(word, result["results"][0]["text"])

    def test_name_search_does_not_center_on_longer_word(self):
        self.body = START + " A halyard hangs." + " filler" * 100 + " Hal arrives." + STOP
        self.write_source(self.body + UNREAD)
        self.anchor()
        for mode in ["all", "phrase"]:
            result = self.request("search", query="Hal", source="text", mode=mode, max_chars=80)
            self.assertIn("Hal arrives", result["results"][0]["text"])
            self.assertEqual(len(result["results"]), 1)

    def test_all_any_modes_and_fts_metacharacters(self):
        self.assertEqual(self.request("search", query="cobalt missingword")["status"], "no_matches")
        self.assertEqual(self.request("search", query="cobalt missingword", mode="any")["status"], "ok")
        self.assertEqual(self.request("search", query='cobalt" OR *', mode="any")["status"], "ok")

    def test_budgets_and_offsets(self):
        for action, args in [("search", {"query": "cobalt", "limit": 20}),
                             ("current", {}), ("page", {"page": 49})]:
            result = self.request(action, max_chars=64, **args)
            self.assertEqual(result["status"], "ok")
            self.assertLessEqual(sum(len(x["text"]) for x in result["results"]), 64)
            for item in result["results"]:
                a, b = item["citation"]["chars"]
                self.assertTrue(0 <= a < b <= len(self.body))

    def test_page_map_is_monotone_and_page_request_is_bounded(self):
        boundary = load_boundary(self.config)
        pages = PageMap(self.config, boundary.documents["text"], 50)
        self.assertEqual(pages.positions, sorted(set(pages.positions)))
        self.assertEqual(pages.pages, sorted(pages.pages))
        self.assertTrue(self.request("page", page=50)["location_is_estimated"])
        self.assertEqual(self.request("page", page=51)["error"]["code"], "outside_bookmark")

    def test_missing_page_map_does_not_disable_text_search(self):
        (self.root / "source/pages.json").unlink()
        self.assertEqual(self.request("search", query="cobalt")["status"], "ok")
        self.assertEqual(self.request("page", page=40)["error"]["code"], "page_map_unavailable")

    def test_uncalibrated_page_is_marked_advisory(self):
        # A book with no calibration can sit at a nominal page forever while the
        # exact boundary advances; the summary must not let that read as progress.
        self.assertNotIn("page_is_advisory", self.request("status")["bookmark"])
        book = json.loads((self.root / ".reading/book.json").read_text())
        book["calibration"] = None
        (self.root / ".reading/book.json").write_text(json.dumps(book))
        summary = ReadingRuntime(load_config(self.root)).request({"action": "status"})["bookmark"]
        self.assertTrue(summary["page_is_advisory"])
        self.assertIn("exact_text", summary["position_truth"])
        self.assertEqual(summary["page"], self.request("status")["bookmark"]["page"])

    def test_translation_requires_independent_boundary(self):
        (self.root / "source/book_zh.txt").write_text("原文不能用英文页码截断。UNREAD_SENTINEL")
        self.assertEqual(self.request("search", query="原文", source="translation")["error"]["code"],
                         "translation_unanchored")
        self.anchor(translation_start="原文不能用英文页码", translation_after="原文不能用英文页码截断。")
        self.assertEqual(self.request("search", query="英文页码", source="translation", mode="phrase")["status"], "ok")
        self.anchor()
        self.assertFalse(self.request("status")["sources"]["translation"]["available"])

    def test_ambiguous_anchor_does_not_disclose_locations(self):
        self.write_source(self.body + STOP + UNREAD)
        with self.assertRaises(ReadingError) as result:
            self.anchor()
        self.assertEqual(result.exception.code, "ambiguous_anchor")
        self.assertNotIn(str(len(self.body)), str(result.exception))

    def test_wiki_never_opens_crossing_or_unbounded_files(self):
        directory = self.root / "source/wiki"
        (directory / "Pages_3-27.txt").write_text("==Page 3==\n\n'''compass'''<br />\nA safe definition.\n")
        for name in ["Pages_27-63.txt", "Pages_87-127.txt", "A.txt", "Errata.txt",
                     "Notes_and_Errata_-_Pages_983-1079.txt"]:
            (directory / name).write_text("UNREAD_SENTINEL compass")
        original = Path.read_bytes
        opened = []
        def spy(path):
            opened.append(path.name)
            return original(path)
        with patch.object(Path, "read_bytes", spy):
            results = list(wiki_passages(self.config, 49))
        self.assertEqual(opened, ["Pages_3-27.txt"])
        self.assertEqual(len(results), 1)

    def test_wiki_future_references_and_unknown_headers_are_excluded(self):
        text = """{{Unbounded template}}
==Page 3==

'''compass'''<br />
A permitted definition.

'''compass'''<br />
UNREAD_SENTINEL see pp. 4–129.

'''compass'''<br />
UNREAD_SENTINEL see [[Pages_87-127|other pages]].

==Page unknown==

'''compass'''<br />
UNREAD_SENTINEL not anchored.

==Page 4==

'''maps'''<br />
A second permitted definition.
"""
        (self.root / "source/wiki/Pages_3-27.txt").write_text(text)
        result = self.request("search", query="compass", source="wiki")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["citation"]["annotation_page"], 3)
        read = self.request("read", reference=result["results"][0]["id"])
        self.assertEqual(read["results"][0]["text"], result["results"][0]["text"])

    def test_wiki_reading_order_uses_pages_across_files(self):
        (self.root / "source/wiki/Pages_3-27.txt").write_text(
            "==Page 20==\n\n'''compass'''<br />\nAn earlier definition.\n")
        (self.root / "source/wiki/Pages_27-63.txt").write_text(
            "==Page 28==\n\n'''compass'''<br />\nA later definition.\n")
        self.anchor(page=70)
        result = self.request("search", query="compass", source="wiki", order="reading")
        self.assertEqual([x["citation"]["annotation_page"] for x in result["results"]], [20, 28])
        result = self.request("search", query="compass", source="wiki", order="recent")
        self.assertEqual([x["citation"]["annotation_page"] for x in result["results"]], [28, 20])

    def test_partial_page_cannot_enable_whole_wiki_range(self):
        path = self.root / "source/wiki/Pages_3-27.txt"
        path.write_text("==Page 27==\n\n'''compass'''<br />\nSafe after completing the page.\n")
        self.anchor(page=27, complete=False)
        self.assertEqual(self.request("search", query="compass", source="wiki")["status"], "no_matches")
        self.anchor(page=27, complete=True)
        self.assertEqual(self.request("search", query="compass", source="wiki")["status"], "ok")

    def test_invalid_json_requests_do_not_crash_runtime(self):
        requests = [None, [], {}, {"action": []}, {"action": "search"},
                    {"action": "search", "query": None}, {"action": "search", "query": "a", "source": []},
                    {"action": "search", "query": "a", "mode": {}},
                    {"action": "search", "query": "a", "limit": True},
                    {"action": "read", "reference": 123},
                    {"action": "page", "page": -10}, {"action": "current", "max_chars": 999999},
                    {"action": "search", "query": "a", "bookmark": 999},
                    {"action": "sync-translation"}]
        for request in requests:
            with self.subTest(request=request):
                self.assertEqual(self.reader.request(request)["status"], "blocked")

    def test_random_cutoffs_and_expansions_never_include_unread_suffix(self):
        randomizer = random.Random(723)
        words = ["a" + hashlib.sha256(str(i).encode()).hexdigest()[:12] for i in range(180)]
        text = START + "\n" + " ".join(words)
        for _ in range(20):
            cutoff = randomizer.randrange(10, 175)
            stop = " ".join(words[cutoff - 2:cutoff])
            (self.root / "source/book_en.txt").write_text(text)
            boundary = set_bookmark(self.config, 50, stop, start=START)
            allowed_end = boundary.documents["text"].end
            result = self.request("search", query=words[cutoff - 1], source="text",
                                  max_chars=randomizer.randrange(64, 400))
            self.assertEqual(result["status"], "ok")
            for item in result["results"]:
                expanded = self.request("read", reference=item["id"], before=6000, after=6000)
                self.assertLessEqual(expanded["results"][0]["citation"]["chars"][1], allowed_end)
                self.assertNotIn(words[cutoff], expanded["results"][0]["text"])

    def test_json_lines_transport(self):
        script = Path(__file__).resolve().parents[1] / "read.py"
        process = subprocess.run(
            [sys.executable, str(script), "--root", str(self.root), "serve"],
            input='{"action":"status"}\nnot json\n{"action":"search","query":"revelation"}\n',
            text=True, capture_output=True, check=True)
        rows = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([row["status"] for row in rows], ["ok", "blocked", "no_matches"])
        self.assertEqual(process.stderr, "")


class TranslationSyncTests(unittest.TestCase):
    """Bilingual synthetic corpus: probes bounded, anchoring forward-only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "source/wiki").mkdir(parents=True)
        write_book(self.root)
        self.en_body = START + "\n" + ("A traveler studies maps and a cobalt compass. " * 80) + STOP
        # A mostly-unread original keeps the proportional bootstrap estimate small.
        self.en_full = self.en_body + " UNREAD_SENTINEL " + "unread filler words " * 3000
        (self.root / "source/book_en.txt").write_text(self.en_full, encoding="utf-8")
        self.zh_body = ZH_START + ZH_EARLY + ZH_PHRASE * 140 + ZH_STOP
        self.zh_full = self.zh_body + ZH_NEXT + ZH_AHEAD * 3000 + " UNREAD_SENTINEL 未读情节"
        (self.root / "source/book_zh.txt").write_text(self.zh_full, encoding="utf-8")
        self.config = load_config(self.root)
        self.bookmark = self.config.bookmark
        self.reader = ReadingRuntime(self.config)
        set_bookmark(self.config, 50, STOP, start=START)

    def state(self):
        return json.loads((self.bookmark).read_text(encoding="utf-8"))

    def sync_translation(self, **kwargs):
        return set_bookmark(self.config, translation_start=ZH_START,
                            translation_after=kwargs.pop("translation_after", ZH_STOP), **kwargs)

    def assertNoUnread(self, payload):
        self.assertNotIn("UNREAD_SENTINEL", json.dumps(payload, ensure_ascii=False))

    def test_bootstrap_probe_windows_stay_inside_the_band(self):
        result = probe(self.config)
        self.assertEqual(result["probe"]["target"], "stop")
        self.assertEqual(result["probe"]["en_offset"], len(self.en_body))
        # No part markers: structural and proportional estimates coincide.
        expected = int(len(self.en_body) * len(self.zh_full) / len(self.en_full))
        self.assertEqual(result["probe"]["zh_estimate"], expected)
        floor, ceiling = result["probe"]["zh_search_band"]
        self.assertEqual(ceiling, min(len(self.zh_full), expected + 40000))
        self.assertLess(floor, self.zh_full.index(ZH_STOP))
        self.assertLessEqual(len(result["probe"]["windows"]), 3)
        for window in result["probe"]["windows"]:
            a, b = window["chars"]
            self.assertTrue(floor <= a < b <= ceiling)
            self.assertLessEqual(len(window["text"]), 2200)
        self.assertNoUnread(result)

    def test_probe_for_start_targets_the_beginning(self):
        result = probe(self.config, target="start")
        self.assertEqual(result["probe"]["en_offset"], 0)
        self.assertIn(ZH_START[:6], result["probe"]["windows"][0]["text"])
        self.assertNoUnread(result)

    def test_probe_shift_and_near_are_bounded(self):
        with self.assertRaises(ReadingError) as result:
            probe(self.config, shift=10 ** 7)
        self.assertEqual(result.exception.code, "window_out_of_range")
        with self.assertRaises(ReadingError) as result:
            probe(self.config, near="未读情节")
        self.assertEqual(result.exception.code, "probe_no_match")
        self.assertNotIn(str(len(self.zh_full)), str(result.exception))
        with self.assertRaises(ReadingError) as result:
            probe(self.config, near="x")
        self.assertEqual(result.exception.code, "invalid_request")
        result = probe(self.config, near="停下来休息")
        self.assertLessEqual(len(result["probe"]["windows"]), 3)
        for window in result["probe"]["windows"]:
            a, b = window["chars"]
            floor, ceiling = result["probe"]["zh_search_band"]
            self.assertTrue(floor <= a < b <= ceiling)
        self.assertNoUnread(result)

    def test_translation_only_update_carries_the_english_boundary(self):
        boundary = self.sync_translation()
        self.assertEqual(boundary.page, 50)
        self.assertEqual(boundary.documents["text"].end, len(self.en_body))
        self.assertEqual(boundary.documents["translation"].end,
                         self.zh_full.index(ZH_STOP) + len(ZH_STOP))
        self.assertEqual(self.state()["translation_sync"],
                         {"last_en_end": len(self.en_body),
                          "last_zh_end": self.zh_full.index(ZH_STOP) + len(ZH_STOP)})
        self.assertEqual(alignment_pairs(self.config),
                         [[len(self.en_body),
                           self.zh_full.index(ZH_STOP) + len(ZH_STOP), 50]])
        self.assertTrue(self.reader.request(
            {"action": "status"})["sources"]["translation"]["available"])
        found = self.reader.request({"action": "search", "query": "停下来休息",
                                     "source": "translation", "mode": "phrase"})
        self.assertEqual(found["status"], "ok")

    def advance_english(self):
        stop2 = "A second stop closes the second sitting."
        body2 = self.en_body + "Further allowed prose fills the page. " * 3 + stop2
        (self.root / "source/book_en.txt").write_text(
            body2 + " UNREAD_SENTINEL " + "unread filler words " * 3000, encoding="utf-8")
        set_bookmark(self.config, 60, stop2)
        return len(body2)

    def test_advance_without_translation_keeps_the_last_pair(self):
        self.sync_translation()
        self.advance_english()
        state = self.state()
        self.assertEqual(set(state["documents"]), {"text"})
        self.assertEqual(state["translation_sync"],
                         {"last_en_end": len(self.en_body),
                          "last_zh_end": self.zh_full.index(ZH_STOP) + len(ZH_STOP)})
        self.assertFalse(self.reader.request(
            {"action": "status"})["sources"]["translation"]["available"])
        blocked = self.reader.request({"action": "search", "query": "停下来",
                                       "source": "translation"})
        self.assertEqual(blocked["error"]["code"], "translation_unanchored")

    def test_re_anchor_after_advance_uses_the_sync_band(self):
        self.sync_translation()
        self.advance_english()
        result = probe(self.config)
        self.assertEqual(result["probe"]["alignment_pairs"], 1)
        floor, ceiling = result["probe"]["zh_search_band"]
        self.assertLessEqual(ceiling - floor, 3000 + 6000)
        self.assertNoUnread(result)
        self.sync_translation(translation_after=ZH_NEXT)
        self.assertEqual(len(alignment_pairs(self.config)), 2)
        self.assertTrue(self.reader.request(
            {"action": "status"})["sources"]["translation"]["available"])
        result = probe(self.config)
        self.assertEqual(result["probe"]["alignment_pairs"], 2)

    def test_translation_rewind_is_rejected(self):
        self.sync_translation()
        with self.assertRaises(ReadingError) as result:
            self.sync_translation(translation_after=ZH_EARLY)
        self.assertEqual(result.exception.code, "translation_rewound")
        self.assertIn("translation", self.state()["documents"])

    def test_english_rewind_after_sync_is_rejected(self):
        self.sync_translation()
        with self.assertRaises(ReadingError) as result:
            set_bookmark(self.config, 45, START)
        self.assertEqual(result.exception.code, "english_bookmark_rewound")
        self.assertIn("translation", self.state()["documents"])

    def test_update_without_quotes_fails_clean_before_initialization(self):
        (self.bookmark).unlink()
        with self.assertRaises(ReadingError) as result:
            set_bookmark(self.config, 50, None)
        self.assertEqual(result.exception.code, "start_required")
        self.assertFalse((self.bookmark).exists())

    def test_config_must_be_present_and_valid(self):
        (self.config.bookmark.parent / "book.json").unlink()
        with self.assertRaises(ReadingError) as result:
            load_config(self.root)
        self.assertEqual(result.exception.code, "config_missing")
        (self.config.bookmark.parent / "book.json").write_text("{}")
        with self.assertRaises(ReadingError) as result:
            load_config(self.root)
        self.assertEqual(result.exception.code, "config_invalid")
        (self.config.bookmark.parent / "book.json").write_text(json.dumps({
            "version": 1, "title": "Escape", "documents": {"text": "../outside.txt"},
            "page_range": [1, 10]}))
        with self.assertRaises(ReadingError) as result:
            load_config(self.root)
        self.assertEqual(result.exception.code, "config_invalid")


class SummaryTests(unittest.TestCase):
    """Runtime-owned running summary: tiling, rollback hiding, staleness, budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_book(self.root)
        (self.root / "source").mkdir(parents=True)
        self.body = START + "\n" + ("A traveler studies maps and a cobalt compass. " * 80) + STOP
        (self.root / "source/book_en.txt").write_text(self.body + UNREAD, encoding="utf-8")
        self.config = load_config(self.root)
        set_bookmark(self.config, 50, STOP, start=START)

    def append(self, text="A quiet recap of the invented traveler so far.", tile=12000):
        with patch.object(summary_module, "TILE", tile):
            return append_summary(self.config, text)

    def read_summary(self, **kwargs):
        return ReadingRuntime(self.config).request({"action": "summary", **kwargs})

    def status(self):
        return ReadingRuntime(self.config).request({"action": "status"})["summary"]

    def set_budget(self, value):
        book = json.loads((self.root / ".reading/book.json").read_text())
        book["summary_budget"] = value
        (self.root / ".reading/book.json").write_text(json.dumps(book))
        self.config = load_config(self.root)

    def test_append_stamps_runtime_coordinates_and_stops_at_bookmark(self):
        result = self.append()
        self.assertEqual(result["appended"]["chars"], [0, len(self.body)])
        self.assertEqual(result["appended"]["level"], 0)
        self.assertTrue(result["caught_up"])
        with self.assertRaises(ReadingError) as again:
            self.append()
        self.assertEqual(again.exception.code, "summary_caught_up")
        entries = self.read_summary()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["chars"], [0, len(self.body)])

    def test_catch_up_tiles_in_reading_order(self):
        result = None
        for expected in range(len(self.body) // 500 + 1):
            result = self.append(tile=500)
            self.assertEqual(result["appended"]["chars"],
                             [expected * 500, min((expected + 1) * 500, len(self.body))])
            if result["caught_up"]:
                break
        self.assertTrue(result["caught_up"])
        entries = self.read_summary()["entries"]
        self.assertEqual([entry["chars"] for entry in entries],
                         [[i, min(i + 500, len(self.body))] for i in range(0, len(self.body), 500)])

    def test_rollback_hides_entries_until_reading_catches_up(self):
        milestone = "A unique milestone sentence appears here."
        body = (START + "\n" + ("A traveler studies maps and a cobalt compass. " * 40)
                + milestone + " " + ("More traveler prose fills the space. " * 40) + STOP)
        (self.root / "source/book_en.txt").write_text(body + UNREAD, encoding="utf-8")
        set_bookmark(self.config, 50, STOP, start=START)
        while True:
            result = self.append(tile=500)
            if result["caught_up"]:
                break
        total = len(self.read_summary()["entries"])
        rolled = set_bookmark(self.config, 49, milestone, start=START)
        rolled_end = rolled.documents["text"].end
        view = self.read_summary()
        self.assertGreater(view["hidden"], 0)
        self.assertEqual(view["hidden"] + len(view["entries"]), total)
        self.assertTrue(all(chars[1] <= rolled_end for chars in
                            (entry["chars"] for entry in view["entries"])))
        set_bookmark(self.config, 50, STOP)
        self.assertEqual(self.read_summary()["hidden"], 0)
        with self.assertRaises(ReadingError) as again:
            self.append()
        self.assertEqual(again.exception.code, "summary_caught_up")

    def test_source_change_makes_summary_stale_until_rebuilt(self):
        self.append()
        (self.root / "source/book_en.txt").write_text(
            self.body + " Further allowed prose closes here." + UNREAD, encoding="utf-8")
        set_bookmark(self.config, 50, "Further allowed prose closes here.")
        view = self.read_summary()
        self.assertEqual(view["error"]["code"], "summary_stale")
        self.assertEqual(self.status(),
                         {"available": False, "reason": "summary_stale", "entries": []})

    def test_over_budget_suggests_merge_and_replace_restores(self):
        self.set_budget(100)
        while True:
            result = self.append(tile=500)
            if result["caught_up"]:
                break
        view = self.read_summary()
        self.assertTrue(view["over_budget"])
        self.assertEqual(view["suggest_merge"], [1, 6])
        backup = self.root / ".reading" / "summary.json.bak"
        self.assertFalse(backup.exists())
        result = replace_summary(self.config, "1-8", 1,
                                 "One coarser recap replaces eight fine entries.")
        self.assertTrue(backup.exists())
        self.assertEqual(len(json.loads(backup.read_text())["entries"]), 8)
        view = self.read_summary()
        self.assertFalse(view["over_budget"])
        self.assertEqual(len(view["entries"]), 1)
        self.assertEqual(view["entries"][0]["level"], 1)
        self.assertEqual(view["entries"][0]["chars"], [0, len(self.body)])

    def test_replace_rejects_bad_requests(self):
        self.append()
        for span, level, text in [("0-1", 1, "A fine replacement entry text."),
                                  ("1-2", 1, "A fine replacement entry text."),
                                  ("1-1", 3, "A fine replacement entry text."),
                                  ("1-1", 1, "too short"),
                                  ("x-y", 1, "A fine replacement entry text.")]:
            with self.subTest(span=span, level=level):
                with self.assertRaises(ReadingError) as raised:
                    replace_summary(self.config, span, level, text)
                self.assertEqual(raised.exception.code, "invalid_request")

    def test_markdown_export_lists_visible_entries(self):
        (self.root / "source/pages.json").write_text(json.dumps({"calib": [[100, 4], [3000, 45]]}))
        self.append(tile=2000)
        view = self.read_summary(markdown=True)
        self.assertIn("## 1. pp. 1–", view["markdown"])
        self.assertIn("A quiet recap", view["markdown"])

    def test_pages_are_null_without_calibration(self):
        self.append()
        self.assertIsNone(self.read_summary()["entries"][0]["pages"])

    def test_status_carries_the_summary_entries_themselves(self):
        empty = self.status()
        self.assertEqual((empty["available"], empty["reason"], empty["entries"]),
                         (False, "empty", []))
        self.append(text="A quiet recap naming the cobalt compass.")
        reported = self.status()
        self.assertTrue(reported["available"])
        self.assertFalse(reported["over_budget"])
        self.assertEqual([entry["text"] for entry in reported["entries"]],
                         ["A quiet recap naming the cobalt compass."])
        self.assertEqual(reported["entries"], self.read_summary()["entries"])

    def test_summary_budget_must_be_an_integer(self):
        book = json.loads((self.root / ".reading/book.json").read_text())
        book["summary_budget"] = "big"
        (self.root / ".reading/book.json").write_text(json.dumps(book))
        with self.assertRaises(ReadingError) as raised:
            load_config(self.root)
        self.assertEqual(raised.exception.code, "config_invalid")

    def test_range_reads_in_order_and_stops_at_the_bookmark(self):
        reader = ReadingRuntime(self.config)
        blocked = reader.request({"action": "range", "from": len(self.body) + 5,
                                  "to": len(self.body) + 100})
        self.assertEqual(blocked["error"]["code"], "outside_bookmark")
        tail = reader.request({"action": "range", "from": len(self.body) - 60, "to": 10 ** 9})
        self.assertEqual(tail["citation"]["chars"], [len(self.body) - 60, len(self.body)])
        self.assertTrue(tail["truncated"])
        self.assertTrue(tail["text"].endswith(STOP))
        head = reader.request({"action": "range", "from": 0, "to": 40, "max_chars": 64})
        self.assertEqual(head["citation"]["chars"], [0, 40])
        self.assertEqual(head["text"], self.body[:40])
        clamped = reader.request({"action": "range", "from": 0, "to": 10 ** 9, "max_chars": 64})
        self.assertEqual(clamped["citation"]["chars"], [0, 64])
        self.assertTrue(clamped["truncated"])
        self.assertNotIn("UNREAD_SENTINEL", json.dumps([blocked, tail, head, clamped]))

    def test_serve_can_read_but_not_write_the_summary(self):
        script = Path(__file__).resolve().parents[1] / "read.py"
        process = subprocess.run(
            [sys.executable, str(script), "--root", str(self.root), "serve"],
            input=('{"action":"summary"}\n'
                   '{"action":"summary","append":"A recap written over the transport."}\n'
                   '{"action":"range","from":0,"to":40}\n'),
            text=True, capture_output=True, check=True)
        rows = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([row["status"] for row in rows], ["ok", "blocked", "ok"])
        self.assertEqual(rows[1]["error"]["code"], "invalid_request")
        self.assertEqual(rows[2]["citation"]["chars"], [0, 40])

    def test_cli_append_and_markdown_export(self):
        script = Path(__file__).resolve().parents[1] / "read.py"
        first = subprocess.run(
            [sys.executable, str(script), "--root", str(self.root), "summary",
             "--append", "A quiet recap of the invented traveler so far."],
            text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(first.stdout)["status"], "ok")
        second = subprocess.run(
            [sys.executable, str(script), "--root", str(self.root), "summary", "--markdown"],
            text=True, capture_output=True, check=True)
        payload = json.loads(second.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("A quiet recap", payload["markdown"])
        status = subprocess.run(
            [sys.executable, str(script), "--root", str(self.root), "status"],
            text=True, capture_output=True, check=True)
        self.assertTrue(json.loads(status.stdout)["summary"]["available"])


if __name__ == "__main__":
    unittest.main()
