"""
Build a manifest of FAS report URLs from the site's XML sitemap.

Akamai fingerprints the TLS handshake, so plain requests gets a 403
from every path on this host. curl_cffi reproduces Chrome's handshake
and is let through.

Stage 1: URLs only. Metadata comes later, off each report page.

Usage:
    python crawl_sitemap.py --peek          # fetch one sitemap page, show samples
    python crawl_sitemap.py                 # full run -> manifest.db
    python crawl_sitemap.py --pattern fresh-deciduous-fruit
"""

import argparse
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

from curl_cffi import requests

BASE = "https://www.fas.usda.gov"
SITEMAP = BASE + "/sitemap.xml"
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DB = "manifest.db"
DELAY = 1.0

# Report slugs that indicate grape-bearing documents. Deliberately wide -
# narrowing happens later, at the section level.
DEFAULT_PATTERN = r"fresh-deciduous-fruit|raisin|table-grape|wine"


def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            url        TEXT PRIMARY KEY,
            slug       TEXT,
            lastmod    TEXT,
            matched    INTEGER,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_matched ON reports(matched)")
    con.commit()
    return con


def get(session, url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.content


def sitemap_pages(session):
    """Return the list of child sitemap URLs from the index."""
    root = ET.fromstring(get(session, SITEMAP))
    return [loc.text for loc in root.findall(".//sm:sitemap/sm:loc", NS)]


def urls_from_page(session, page_url):
    """Return (url, lastmod) pairs from one child sitemap."""
    root = ET.fromstring(get(session, page_url))
    out = []
    for url_el in root.findall(".//sm:url", NS):
        loc = url_el.find("sm:loc", NS)
        mod = url_el.find("sm:lastmod", NS)
        if loc is not None:
            out.append((loc.text, mod.text if mod is not None else None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", default=DEFAULT_PATTERN,
                    help="regex matched against the URL slug")
    ap.add_argument("--peek", action="store_true",
                    help="fetch the index and one page, print samples, exit")
    args = ap.parse_args()

    session = requests.Session(impersonate="chrome124")
    session.headers.update(HEADERS)

    pattern = re.compile(args.pattern, re.I)

    try:
        pages = sitemap_pages(session)
    except Exception as e:
        print(f"could not read sitemap index: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"sitemap index lists {len(pages)} pages")

    if args.peek:
        sample = urls_from_page(session, pages[0])
        print(f"\npage 1 holds {len(sample)} urls. first 10:\n")
        for u, m in sample[:10]:
            print(" ", u)
        hits = [u for u, _ in sample if pattern.search(u)]
        print(f"\n{len(hits)} of them match /{args.pattern}/")
        for u in hits[:10]:
            print("  MATCH", u)
        return

    con = init_db()
    seen = total = 0

    for i, page_url in enumerate(pages, 1):
        try:
            entries = urls_from_page(session, page_url)
        except Exception as e:
            print(f"page {i}: {e} - skipping", file=sys.stderr)
            continue

        rows = []
        for url, lastmod in entries:
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            rows.append((url, slug, lastmod, 1 if pattern.search(url) else 0))

        con.executemany(
            "INSERT OR IGNORE INTO reports (url, slug, lastmod, matched) "
            "VALUES (?, ?, ?, ?)", rows)
        con.commit()

        seen += len(rows)
        total = con.execute(
            "SELECT COUNT(*) FROM reports WHERE matched=1").fetchone()[0]
        print(f"page {i}/{len(pages)}: {len(rows)} urls, "
              f"{total} matches so far")
        time.sleep(DELAY)

    print(f"\ndone - {seen} urls scanned, {total} matched")
    print("\nsample matches:")
    for (u,) in con.execute(
            "SELECT url FROM reports WHERE matched=1 LIMIT 10"):
        print(" ", u)


if __name__ == "__main__":
    main()