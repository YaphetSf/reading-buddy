#!/usr/bin/env python3
"""Mirror a book's page-range annotation wiki into a local raw-wikitext cache.

Book-agnostic: the annotation source, its page ranges and the cache directory
all come from the workspace's .reading/book.json. A book without an
"annotations" block has nothing to fetch.

Pure cache. Having the files locally does not relax the spoiler protocol: the
runtime opens an annotation file only when its whole declared range is already
read, and never opens titles listed under extra_titles or found by --discover,
which carry no enforced reading boundary.

Usage:
  python3 fetch_wiki.py                  # fetch missing range pages only
  python3 fetch_wiki.py --force          # re-download everything
  python3 fetch_wiki.py --discover       # also sweep the API for index/errata pages
  python3 fetch_wiki.py --root <path>    # workspace to operate on (default: cwd)
"""
import argparse
import json
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request

UA = "reading-buddy wiki mirror/1.0 (personal offline study use)"


def http_get(url):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def load_annotations(root):
    try:
        book = json.loads((root / ".reading" / "book.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        sys.exit(f"Cannot read {root}/.reading/book.json: {error}")
    annotations = book.get("annotations")
    if not annotations:
        sys.exit(f"{book.get('title', 'This book')} declares no annotation source; nothing to fetch.")
    return annotations


def discover_extra_titles(api):
    """Index and errata style pages, which the runtime never opens."""
    found = []
    url = api + "?" + urllib.parse.urlencode(
        {"action": "query", "list": "allpages", "aplimit": "500", "format": "json"})
    try:
        for page in json.loads(http_get(url))["query"]["allpages"]:
            title = page["title"]
            if title.startswith("Errata") or re.fullmatch(r"[A-Z]", title):
                found.append(title)
    except Exception as error:
        print(f"(API discovery skipped: {error})")
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="Book workspace holding .reading/book.json (default: cwd).")
    parser.add_argument("--force", action="store_true", help="Re-download pages already cached.")
    parser.add_argument("--discover", action="store_true",
                        help="Also sweep the wiki API for unbounded index and errata pages.")
    args = parser.parse_args()

    annotations = load_annotations(args.root)
    url_base = annotations["url_base"]
    out = args.root / annotations["directory"]

    titles = [f"Pages {low}-{high}" for low, high in annotations["ranges"]]
    titles += list(annotations.get("extra_titles", {}))
    if args.discover:
        titles += discover_extra_titles(url_base.split("index.php")[0] + "api.php")

    # A title may be a shell page that only transcludes a template.
    alias = dict(annotations.get("extra_titles", {}))

    out.mkdir(parents=True, exist_ok=True)
    ok = fail = skip = 0
    for title in titles:
        path = out / (title.replace(" ", "_") + ".txt")
        if path.exists() and path.stat().st_size > 200 and not args.force:
            skip += 1
            continue
        url = url_base + urllib.parse.quote(alias.get(title) or title) + "&action=raw"
        try:
            text = http_get(url)
            if len(text) < 100:
                raise RuntimeError(f"empty response ({len(text)} chars)")
            path.write_text(text, encoding="utf-8")
            print(f"  ok   {title}  ({len(text):,} chars)")
            ok += 1
        except Exception as error:
            print(f"  FAIL {title}: {error}")
            fail += 1
        time.sleep(0.4)
    print(f"\n{ok} fetched, {skip} already present, {fail} failed -> {out}")


if __name__ == "__main__":
    main()
