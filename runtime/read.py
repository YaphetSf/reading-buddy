#!/usr/bin/env python3
"""CLI and JSON-lines transport for the local reading runtime."""

import argparse
import json
from pathlib import Path
import sys

from reading.boundary import ReadingError, set_bookmark
from reading.config import load_config
from reading.runtime import ReadingRuntime
from reading.sync import probe


def emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="Book workspace holding .reading/book.json (default: cwd).")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="Validate the bookmark and report source availability.")
    search = sub.add_parser("search", help="Search only text already read.")
    search.add_argument("query")
    search.add_argument("--source", choices=["all", "text", "wiki", "translation"], default="all")
    search.add_argument("--mode", choices=["all", "any", "phrase"], default="all")
    search.add_argument("--order", choices=["relevance", "reading", "recent"], default="relevance")
    search.add_argument("--limit", type=int, default=6)
    read = sub.add_parser("read", help="Expand a returned passage id inside the bookmark.")
    read.add_argument("reference")
    read.add_argument("--before", type=int, default=0, help="Additional original characters.")
    read.add_argument("--after", type=int, default=0, help="Additional original characters.")
    current = sub.add_parser("current", help="Return the last part of the text already read.")
    current.add_argument("--source", choices=["text", "translation"], default="text")
    page = sub.add_parser("page", help="Find an estimated page location inside the bookmark.")
    page.add_argument("page", type=int)
    for command, default in [(search, 6000), (read, 6000), (current, 2500), (page, 2500)]:
        command.add_argument("--max-chars", type=int, default=default,
                             help="Total returned excerpt characters, excluding JSON metadata.")
    bookmark = sub.add_parser("bookmark", help="Explicitly set the last position the user has read.")
    bookmark.add_argument("--page", type=int,
                          help="New last page; omitted when only re-anchoring the translation.")
    bookmark.add_argument("--after", help="Exact last-read quotation, inclusive; omitted to carry it forward.")
    bookmark.add_argument("--start", help="First body-text quotation; required when initializing.")
    bookmark.add_argument("--page-complete", action="store_true",
                          help="Only when the entire indicated page has actually been read.")
    bookmark.add_argument("--translation-after")
    bookmark.add_argument("--translation-start")
    sync = sub.add_parser("sync-translation",
                          help="Bounded translation probes for re-anchoring after an English update.")
    sync.add_argument("--for", dest="target", choices=["start", "stop"], default="stop",
                      help="Anchor the first body-text quotation or the last-read English words.")
    sync.add_argument("--shift", type=int, default=0,
                      help="Move the probe window forward inside the search band.")
    sync.add_argument("--near", help="Exact Chinese phrase to locate inside the search band.")
    sub.add_parser("serve", help="Read one retrieval request per stdin line; emit one JSON response.")
    args = vars(parser.parse_args())
    root, action = args.pop("root"), args.pop("action")
    try:
        config = load_config(root)
        runtime = ReadingRuntime(config)
    except ReadingError as exc:
        emit({"schema_version": 1, "status": "blocked",
              "error": {"code": exc.code, "message": str(exc)}})
        return 2
    if action == "serve":
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except ValueError:
                emit({"schema_version": 1, "status": "blocked",
                      "error": {"code": "invalid_json", "message": "Expected one JSON request per line."}})
                continue
            emit(runtime.request(request))
            sys.stdout.flush()
        return 0
    if action == "bookmark":
        try:
            boundary = set_bookmark(config, **args)
            response = {"schema_version": 1, "status": "ok", "bookmark": boundary.summary()}
        except ReadingError as exc:
            response = {"schema_version": 1, "status": "blocked",
                        "error": {"code": exc.code, "message": str(exc)}}
    elif action == "sync-translation":
        # A bookmark-adjacent operation, deliberately outside the retrieval transport.
        try:
            response = {"schema_version": 1, "status": "ok", **probe(config, **args)}
        except ReadingError as exc:
            response = {"schema_version": 1, "status": "blocked",
                        "error": {"code": exc.code, "message": str(exc)}}
    else:
        response = runtime.request({"action": action, **args})
    emit(response)
    return 2 if response["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
