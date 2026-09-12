"""
Turn the raw sitemap URLs into a structured report table.

GAIN urls carry their own metadata:
    /data/gain/2026/01/china-raisins-annual
               ^^^^ ^^ ^^^^^ ^^^^^^^^^^^^^
               year mon country  report name

Reads manifest.db (from crawl_sitemap.py), writes a `gain` table and a
`corpus` view holding the subset you actually intend to download.

Usage:
    python build_metadata.py              # parse everything, report on it
    python build_metadata.py --from 2018  # set the year floor for the corpus
"""

import argparse
import re
import sqlite3

DB = "manifest.db"

# Countries whose names contain hyphens, so the naive split on the first
# hyphen would truncate them. Longest first so the match is greedy.
MULTIWORD = [
    "european-union", "united-kingdom", "united-arab-emirates",
    "south-africa", "saudi-arabia", "new-zealand", "costa-rica",
    "dominican-republic", "el-salvador", "burkina-faso", "cote-divoire",
    "sri-lanka", "south-korea", "korea-republic-of", "north-macedonia",
    "bosnia-and-herzegovina", "trinidad-and-tobago", "papua-new-guinea",
    "czech-republic", "hong-kong", "sierra-leone", "cabo-verde",
]

URL_RE = re.compile(r"/data/gain/(\d{4})/(\d{2})/([a-z0-9-]+)$")

# What counts as grape-bearing. Fresh deciduous fruit annuals carry the
# table grape section; raisins and table grape reports are direct hits.
INCLUDE = re.compile(r"fresh-deciduous-fruit|table-grape|raisin", re.I)

# Processed fruit shares the "deciduous" word but has no grape content.
EXCLUDE = re.compile(r"canned|dried-prunes|juice", re.I)


def split_country(slug):
    for c in sorted(MULTIWORD, key=len, reverse=True):
        if slug.startswith(c + "-"):
            return c, slug[len(c) + 1:]
    country, _, rest = slug.partition("-")
    return country, rest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="year_from", type=int, default=2018)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.execute("DROP TABLE IF EXISTS gain")
    con.execute("""
        CREATE TABLE gain (
            url         TEXT PRIMARY KEY,
            year        INTEGER,
            month       INTEGER,
            country     TEXT,
            report_name TEXT,
            slug        TEXT
        )
    """)

    rows = []
    for (url,) in con.execute("SELECT url FROM reports"):
        m = URL_RE.search(url)
        if not m:
            continue
        year, month, slug = m.group(1), m.group(2), m.group(3)
        country, report_name = split_country(slug)
        rows.append((url, int(year), int(month), country, report_name, slug))

    con.executemany(
        "INSERT OR IGNORE INTO gain VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute("CREATE INDEX idx_gain_year ON gain(year)")
    con.execute("CREATE INDEX idx_gain_country ON gain(country)")
    con.commit()

    print(f"parsed {len(rows)} gain reports")

    # The download target
    con.execute("DROP TABLE IF EXISTS corpus")
    con.execute("""
        CREATE TABLE corpus AS
        SELECT * FROM gain
        WHERE year >= ?
          AND (report_name LIKE '%fresh-deciduous-fruit%'
               OR report_name LIKE '%table-grape%'
               OR report_name LIKE '%raisin%')
          AND report_name NOT LIKE '%canned%'
          AND report_name NOT LIKE '%juice%'
          AND report_name NOT LIKE '%dried-prunes%'
    """, (args.year_from,))
    con.commit()

    n = con.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
    print(f"corpus: {n} reports from {args.year_from} onward\n")

    print("by country:")
    for country, c in con.execute(
            "SELECT country, COUNT(*) FROM corpus "
            "GROUP BY country ORDER BY COUNT(*) DESC LIMIT 15"):
        print(f"  {c:4d}  {country}")

    print("\nby year:")
    for year, c in con.execute(
            "SELECT year, COUNT(*) FROM corpus GROUP BY year ORDER BY year"):
        print(f"  {year}  {'#' * c} {c}")

    print("\nreport names in corpus:")
    for name, c in con.execute(
            "SELECT report_name, COUNT(*) FROM corpus "
            "GROUP BY report_name ORDER BY COUNT(*) DESC LIMIT 12"):
        print(f"  {c:4d}  {name}")


if __name__ == "__main__":
    main()