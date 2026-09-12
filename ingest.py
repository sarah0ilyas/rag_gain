"""
Read every downloaded PDF and build the authoritative `documents` table.

The GAIN cover page carries a labelled metadata block:

    Report Number:    PE2025-0027
    Report Name:      Fresh Deciduous Fruit Annual
    Country:          Peru
    Post:             Lima
    Report Category:  Fresh Deciduous Fruit
    Date:             December 02, 2025

That is more reliable than anything derived from the url slug, so it
wins wherever the two disagree.

Also records a grape-mention count per document. Not every Fresh
Deciduous Fruit Annual covers table grapes - Argentina and New Zealand
report apples and pears only - so report type alone does not tell you
whether a document is in scope.

Usage:
    python ingest.py              # process everything
    python ingest.py --report     # just print the summary
"""

import argparse
import glob
import os
import re
import sqlite3
from datetime import datetime

import fitz

DB = "manifest.db"
PDF_DIR = "pdfs"

FIELDS = ["Report Number", "Report Name", "Country", "Post",
          "Report Category", "Prepared By", "Approved By", "Date"]

GRAPE_RE = re.compile(r"\bgrapes?\b|\braisins?\b|\bvineyard", re.I)
TABLE_GRAPE_RE = re.compile(r"\btable grapes?\b", re.I)


def clean(v):
    return re.sub(r"\s+", " ", v).strip() if v else None


# The same country is spelled several ways across 17 years of filings.
COUNTRY_FIX = {
    "china - peoples republic of": "China",
    "china - people's republic of": "China",
    "korea - republic of": "South Korea",
    "south africa - republic of": "South Africa",
    "turkiye": "Turkey",
    "russian federation": "Russia",
    "burma - union of": "Myanmar",
}


def normalise_country(c):
    if not c:
        return None
    return COUNTRY_FIX.get(c.strip().lower(), c.strip())


def cover_metadata(text):
    """Pull the labelled block off the cover page.

    Post-2018 filings put the value inline after the label. Older ones
    sometimes wrap it onto the next line, so fall back to that when the
    inline capture comes back empty.
    """
    out = {}
    head = text[:4000]          # cover page only, avoid body-text collisions
    for f in FIELDS:
        key = f.lower().replace(" ", "_")
        m = re.search(rf"{f}\s*:\s*(.*)", head)
        if not m:
            out[key] = None
            continue
        val = clean(m.group(1))
        if not val:
            after = head[m.end():].lstrip("\n")
            val = clean(after.split("\n")[0]) if after else None
        out[key] = val or None
    return out


def parse_date(s):
    if not s:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def init(con):
    con.execute("DROP TABLE IF EXISTS documents")
    con.execute("""
        CREATE TABLE documents (
            local_path      TEXT PRIMARY KEY,
            report_number   TEXT,
            report_name     TEXT,
            country         TEXT,
            post            TEXT,
            report_category TEXT,
            published       TEXT,
            year            INTEGER,
            prepared_by     TEXT,
            pages           INTEGER,
            chars           INTEGER,
            grape_hits      INTEGER,
            table_grape_hits INTEGER,
            has_cover_meta  INTEGER,
            text            TEXT
        )
    """)
    con.execute("CREATE INDEX idx_doc_country ON documents(country)")
    con.execute("CREATE INDEX idx_doc_year ON documents(year)")
    con.commit()


def ingest(con):
    paths = sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf")))
    print(f"reading {len(paths)} pdfs")

    rows, failed = [], []
    for i, path in enumerate(paths, 1):
        try:
            doc = fitz.open(path)
            text = "\n".join(p.get_text() for p in doc)
            meta = cover_metadata(text)
            published = parse_date(meta.get("date"))

            rows.append((
                path,
                meta.get("report_number"),
                meta.get("report_name"),
                normalise_country(meta.get("country")),
                meta.get("post"),
                meta.get("report_category"),
                published,
                int(published[:4]) if published else None,
                meta.get("prepared_by"),
                len(doc),
                len(text),
                len(GRAPE_RE.findall(text)),
                len(TABLE_GRAPE_RE.findall(text)),
                1 if meta.get("report_number") else 0,
                text,
            ))
        except Exception as e:
            failed.append((path, str(e)))
            print(f"  FAILED {path}: {e}")

        if i % 50 == 0:
            print(f"  {i}/{len(paths)}")

    con.executemany(
        "INSERT OR REPLACE INTO documents VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()

    stored, = con.execute("SELECT COUNT(*) FROM documents").fetchone()
    print(f"\nread {len(paths)}, parsed {len(rows)}, "
          f"stored {stored}, failed {len(failed)}")
    if stored != len(rows):
        print(f"  MISMATCH: {len(rows) - stored} rows lost to key "
              f"collisions - do not trust this table")


def report(con):
    q = con.execute

    n, = q("SELECT COUNT(*) FROM documents").fetchone()
    nometa, = q("SELECT COUNT(*) FROM documents "
                "WHERE has_cover_meta=0").fetchone()
    print(f"{n} documents, {nometa} missing cover metadata\n")

    print("grape coverage by country:")
    print(f"  {'country':30s} {'docs':>5s} {'w/grapes':>9s} {'median hits':>12s}")
    for country, docs, withg, med in q("""
        SELECT country,
               COUNT(*),
               SUM(CASE WHEN grape_hits > 5 THEN 1 ELSE 0 END),
               CAST(AVG(grape_hits) AS INT)
        FROM documents GROUP BY country
        ORDER BY SUM(CASE WHEN grape_hits > 5 THEN 1 ELSE 0 END) DESC
        LIMIT 20
    """):
        print(f"  {(country or '?'):30s} {docs:5d} {withg:9d} {med:12d}")

    print("\nextraction quality:")
    for label, cond in [("empty (likely scanned)", "chars < 500"),
                        ("thin (<5k chars)", "chars BETWEEN 500 AND 5000"),
                        ("normal", "chars > 5000")]:
        c, = q(f"SELECT COUNT(*) FROM documents WHERE {cond}").fetchone()
        print(f"  {label:26s} {c}")

    print("\nin-scope documents (grape_hits > 5):")
    c, = q("SELECT COUNT(*) FROM documents WHERE grape_hits > 5").fetchone()
    print(f"  {c} of {n}")

    print("\nby year, in scope:")
    for year, c in q("""
        SELECT year, COUNT(*) FROM documents
        WHERE grape_hits > 5 AND year IS NOT NULL
        GROUP BY year ORDER BY year
    """):
        print(f"  {year}  {'#' * c} {c}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    if not args.report:
        init(con)
        ingest(con)
    report(con)


if __name__ == "__main__":
    main()