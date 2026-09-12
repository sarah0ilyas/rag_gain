"""
Download the PDFs for everything in the `corpus` table.

Two passes, both resumable, both recording failures rather than dying:

  resolve  - fetch each landing page, find the PDF href, parse the
             post and report number out of the filename
  fetch    - download each resolved PDF to pdfs/

Files are named from the landing-page path, which is unique by
construction. An earlier version named them by the report number parsed
out of the pdf filename; that field is null for ~80% of filings, so
hundreds of downloads collided onto the same name and the run reported
381 successes while holding 139 files. `status` now counts what is
actually on disk.

Akamai fingerprints TLS, so every request goes through curl_cffi.

Usage:
    python download_pdfs.py resolve
    python download_pdfs.py fetch
    python download_pdfs.py status
"""

import argparse
import os
import re
import sqlite3
import sys
import time
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests

DB = "manifest.db"
PDF_DIR = "pdfs"
BASE = "https://www.fas.usda.gov"
DELAY = 1.5

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/data",
}

# Fresh Deciduous Fruit Annual_Santiago_Chile_CI2025-0028.pdf
# Two filename families:
#   ..._Berlin_European Union_E42023-0045.pdf   (report number)
#   ..._Santiago_Chile_11-01-2021.pdf           (date, pre-2022)
# The trailing field is a hint only - the cover page is authoritative.
FNAME_RE = re.compile(
    r"^(?P<title>.+?)_(?P<post>[^_]+)_(?P<country>[^_]+)_"
    r"(?P<tail>[^_]+)\.pdf$"
)


def init(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS pdfs (
            url           TEXT PRIMARY KEY,   -- landing page
            pdf_url       TEXT,
            title         TEXT,
            post          TEXT,
            country_full  TEXT,
            report_number TEXT,
            local_path    TEXT,
            bytes         INTEGER,
            status        TEXT,               -- resolved|fetched|error
            error         TEXT
        )
    """)
    con.commit()


def session():
    s = requests.Session(impersonate="chrome124")
    s.headers.update(HEADERS)
    return s


def find_pdf_link(html):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower() and "gain-report" in href.lower():
            return urljoin(BASE, href)
    # fall back to any pdf on the page
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(".pdf"):
            return urljoin(BASE, a["href"])
    return None


RN_RE = re.compile(r"^[A-Z0-9]{2}\d{4}-\d{4}$")


def parse_filename(pdf_url):
    fname = unquote(pdf_url.rsplit("/", 1)[-1])
    m = FNAME_RE.match(fname)
    if not m:
        return {"title": fname[:-4] if fname.endswith(".pdf") else fname,
                "post": None, "country": None, "report_number": None}
    d = m.groupdict()
    tail = d.pop("tail")
    d["report_number"] = tail if RN_RE.match(tail) else None
    return d


def resolve(con, limit=None):
    s = session()
    todo = con.execute("""
        SELECT c.url FROM corpus c
        LEFT JOIN pdfs p ON p.url = c.url
        WHERE p.url IS NULL OR p.status = 'error'
        ORDER BY c.year DESC, c.month DESC
    """).fetchall()
    if limit:
        todo = todo[:limit]

    print(f"resolving {len(todo)} landing pages")
    for i, (url,) in enumerate(todo, 1):
        try:
            r = s.get(url, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            pdf_url = find_pdf_link(r.text)
            if not pdf_url:
                raise RuntimeError("no pdf link on page")
            f = parse_filename(pdf_url)
            con.execute("""
                INSERT OR REPLACE INTO pdfs
                    (url, pdf_url, title, post, country_full,
                     report_number, status, error)
                VALUES (?, ?, ?, ?, ?, ?, 'resolved', NULL)
            """, (url, pdf_url, f["title"], f["post"], f["country"],
                  f["report_number"]))
        except Exception as e:
            con.execute("""
                INSERT OR REPLACE INTO pdfs (url, status, error)
                VALUES (?, 'error', ?)
            """, (url, str(e)))
            print(f"  [{i}] ERROR {url}: {e}", file=sys.stderr)

        con.commit()
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(DELAY)

    ok = con.execute(
        "SELECT COUNT(*) FROM pdfs WHERE status='resolved'").fetchone()[0]
    print(f"resolved {ok}")


def fetch(con, limit=None):
    os.makedirs(PDF_DIR, exist_ok=True)
    s = session()
    todo = con.execute("""
        SELECT url, pdf_url, report_number FROM pdfs
        WHERE status = 'resolved'
    """).fetchall()
    if limit:
        todo = todo[:limit]

    print(f"fetching {len(todo)} pdfs")
    for i, (url, pdf_url, rn) in enumerate(todo, 1):
        # Name by the landing-page path, not by anything parsed out of
        # the pdf filename. /data/gain/2021/11/chile-fresh-deciduous-
        # fruit-annual -> 2021-11-chile-fresh-deciduous-fruit-annual.
        # Unique by construction; the authoritative report number comes
        # off the cover page at ingest time.
        parts = url.rstrip("/").split("/data/gain/")[-1].split("/")
        stem = "-".join(parts)[:120]
        path = os.path.join(PDF_DIR, f"{stem}.pdf")

        if os.path.exists(path) and os.path.getsize(path) > 1000:
            con.execute("UPDATE pdfs SET status='fetched', local_path=?, "
                        "bytes=? WHERE url=?",
                        (path, os.path.getsize(path), url))
            con.commit()
            continue

        try:
            r = s.get(pdf_url, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            if not r.content.startswith(b"%PDF"):
                raise RuntimeError("response is not a pdf")
            with open(path, "wb") as fh:
                fh.write(r.content)
            con.execute("UPDATE pdfs SET status='fetched', local_path=?, "
                        "bytes=?, error=NULL WHERE url=?",
                        (path, len(r.content), url))
        except Exception as e:
            con.execute("UPDATE pdfs SET status='error', error=? WHERE url=?",
                        (str(e), url))
            print(f"  [{i}] ERROR {pdf_url}: {e}", file=sys.stderr)

        con.commit()
        if i % 25 == 0:
            done = con.execute(
                "SELECT COUNT(*) FROM pdfs WHERE status='fetched'"
            ).fetchone()[0]
            print(f"  {i}/{len(todo)}  ({done} on disk)")
        time.sleep(DELAY)


def status(con):
    import glob

    print("pdfs table:")
    for st, n in con.execute(
            "SELECT status, COUNT(*) FROM pdfs GROUP BY status"):
        print(f"  {st:10s} {n}")

    total = con.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
    mb = (con.execute("SELECT SUM(bytes) FROM pdfs").fetchone()[0] or 0) / 1e6
    claimed = con.execute(
        "SELECT COUNT(*) FROM pdfs WHERE status='fetched'").fetchone()[0]
    on_disk = len(glob.glob(os.path.join(PDF_DIR, "*.pdf")))

    print(f"\ncorpus target: {total}")
    print(f"claimed:       {claimed}")
    print(f"on disk:       {on_disk}")
    print(f"downloaded:    {mb:.1f} MB")

    if claimed != on_disk:
        print(f"\n  MISMATCH: db claims {claimed}, disk has {on_disk}. "
              f"Filenames are colliding - do not trust the db.")

    errs = con.execute(
        "SELECT error, COUNT(*) FROM pdfs WHERE status='error' "
        "GROUP BY error ORDER BY COUNT(*) DESC LIMIT 5").fetchall()
    if errs:
        print("\nerrors:")
        for e, n in errs:
            print(f"  {n:4d}  {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["resolve", "fetch", "status"])
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    init(con)

    if args.stage == "resolve":
        resolve(con, args.limit)
    elif args.stage == "fetch":
        fetch(con, args.limit)
    else:
        status(con)


if __name__ == "__main__":
    main()