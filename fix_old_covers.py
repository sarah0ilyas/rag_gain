"""
Recover cover-page metadata from pre-2018 GAIN reports.

Post-2018 covers are "Label: value" inline and parse with a plain regex
in ingest.py. Older covers are form layouts: PyMuPDF emits the labels
first with empty values, then the actual values as a block at the end of
the page's text stream. The label-value association is spatial, not
textual, so it has to be recovered from word coordinates.

There are at least two old templates. One puts the date and report
number in a right-hand column (x ~ 470); the other left-aligns everything
(x ~ 150). So column position is not reliable.

What IS consistent, once words are grouped into visual lines:

    "Date: 5/14/2010"                 label and value on one line
    "Approved By:"                    label alone, value on the line below
    "GAIN Report Number:"             label alone, value absent entirely
    "Argentina"                       unlabelled
    "Fresh Deciduous Fruit Semi-annual"   unlabelled
    "2010" / "Apple, Table Grape..."      unlabelled title

So: group words into lines, split labelled lines on the colon, fall back
to the next line when nothing follows it, and read the remaining
unlabelled lines in order - country, category, title. Reports with no
recoverable country are left alone and reported.

Usage:
    python fix_old_covers.py --dry-run          # show what would change
    python fix_old_covers.py --dry-run --limit 5
    python fix_old_covers.py                    # apply the updates
"""

import argparse
import re
import sqlite3
import sys
from datetime import datetime

import fitz

DB = "manifest.db"

# Two words are on the same visual line if their y differs by less than this.
LINE_TOL = 3.0

# Old numbers: CI1218, TW10026, CH16056. New: CI2019-0014, E42023-0045.
RN_RE = re.compile(r"^[A-Z0-9]{2}\d{2,6}(-\d{4})?$")


def lines_with_positions(page):
    """Group a page's words into visual lines.

    Returns [(y, x_of_first_word, "joined text"), ...] sorted by y.
    """
    words = page.get_text("words")
    if not words:
        return []

    rows = {}
    for x0, y0, x1, y1, word, *_ in words:
        key = round(y0 / LINE_TOL)
        rows.setdefault(key, []).append((x0, y0, word))

    out = []
    for key in sorted(rows):
        items = sorted(rows[key])
        y = min(i[1] for i in items)
        x = items[0][0]
        out.append((y, x, " ".join(i[2] for i in items)))
    return out


# Every label that appears on an old cover page. Lines containing any of
# these are labelled lines, and are excluded from the unlabelled block.
LABELS = [
    r"Date:",
    r"(GAIN\s+)?Report\s+Number:",
    r"Approved\s+By:",
    r"Prepared\s+By:",
    r"Report\s+Highlights:",
    r"Required\s+Report",
    r"THIS REPORT CONTAINS",
    r"Voluntary",
]
LABEL_RE = re.compile("|".join(LABELS), re.I)


def labelled_value(lines, pattern, max_gap=25):
    """Value for a "Label: value" field.

    Handles both shapes seen in the corpus: the value on the same line
    after the colon, or the label alone with the value on the next line.
    Returns None when the field is absent, which happens - some old
    reports carry no report number at all.
    """
    rx = re.compile(pattern, re.I)
    for i, (y, x, text) in enumerate(lines):
        m = rx.search(text)
        if not m:
            continue

        # same line, after the colon
        tail = text[m.end():].lstrip(": ").strip()
        if tail:
            return tail

        # next line down, if it is close enough and not another label
        for y2, x2, t2 in lines[i + 1:]:
            if y2 <= y:
                continue
            if y2 - y > max_gap:
                break
            if t2.strip() and not LABEL_RE.search(t2):
                return t2.strip()
            break
    return None


def find_label(lines, pattern):
    """Return (y, x, text) of the first line matching pattern, or None."""
    rx = re.compile(pattern, re.I)
    for y, x, text in lines:
        if rx.search(text):
            return (y, x, text)
    return None


def parse_date(s):
    if not s:
        return None
    s = re.sub(r",(?=\d)", ", ", s.strip())
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_cover(page):
    """Recover what we can from an old-style cover page."""
    lines = lines_with_positions(page)
    if not lines:
        return {}

    out = {"report_number": None, "country": None, "report_category": None,
           "report_name": None, "published": None,
           "prepared_by": None, "approved_by": None}

    # --- labelled fields, whichever shape they take
    out["published"] = parse_date(labelled_value(lines, r"\bDate:"))
    out["approved_by"] = labelled_value(lines, r"Approved\s+By:")
    out["prepared_by"] = labelled_value(lines, r"Prepared\s+By:")

    rn = labelled_value(lines, r"Report\s+Number:")
    if rn and RN_RE.match(rn.strip()):
        out["report_number"] = rn.strip()

    # --- the unlabelled block: country, then category, then title
    # It sits between the distribution notice near the top of the cover and
    # the "Approved By:" label lower down. Everything in that band that is
    # not itself a label belongs to the block, read top to bottom.
    lo_anchor = find_label(lines, r"Required\s+Report|public distribution")
    hi_anchor = find_label(lines, r"Approved\s+By:|Prepared\s+By:")
    lo = lo_anchor[0] if lo_anchor else 240
    hi = hi_anchor[0] if hi_anchor else 490

    block = []
    for y, x, t in lines:
        t = t.strip()
        if not (lo < y < hi) or not t:
            continue
        if LABEL_RE.search(t):          # a labelled line, already handled
            continue
        if RN_RE.match(t):              # a bare report number
            continue
        if parse_date(t):               # a bare date
            continue
        block.append((y, t))

    block.sort()

    if len(block) >= 1:
        out["country"] = block[0][1]
    if len(block) >= 2:
        out["report_category"] = block[1][1]
    if len(block) >= 3:
        out["report_name"] = " ".join(t for _, t in block[2:])

    return out


COUNTRY_FIX = {
    "eu-27": "European Union",
    "eu-28": "European Union",
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change without writing")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    con = sqlite3.connect(DB)

    todo = con.execute("""
        SELECT local_path, country, published FROM documents
        WHERE country IS NULL OR published IS NULL
        ORDER BY local_path
    """).fetchall()
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(todo)} documents missing country or date\n")

    fixed = partial = failed = 0

    for path, have_country, have_published in todo:
        try:
            page = fitz.open(path)[0]
            meta = parse_cover(page)
        except Exception as e:
            print(f"  ERROR {path}: {e}", file=sys.stderr)
            failed += 1
            continue

        # Keep whatever ingest already got right. This parser reads an old
        # form layout; on a modern cover it will happily return the report
        # name as the country, so its output must never win.
        country = have_country or normalise_country(meta.get("country"))
        published = have_published or meta.get("published")

        if country and published:
            fixed += 1
        elif country or published:
            partial += 1
        else:
            failed += 1
            print(f"  no metadata recovered: {path}")
            continue

        if args.dry_run:
            print(f"  {path.split('/')[-1][:46]:48s} "
                  f"{(country or '-'):22s} {(published or '-')}")
            continue

        # documents has no approved_by column, so that field is parsed
        # but not stored. COALESCE means an existing value always wins -
        # this only ever fills in nulls.
        con.execute("""
            UPDATE documents SET
                country         = COALESCE(?, country),
                published       = COALESCE(?, published),
                year            = COALESCE(?, year),
                report_number   = COALESCE(report_number, ?),
                report_name     = COALESCE(report_name, ?),
                report_category = COALESCE(report_category, ?),
                prepared_by     = COALESCE(prepared_by, ?)
            WHERE local_path = ?
        """, (country, published,
              int(published[:4]) if published else None,
              meta.get("report_number"), meta.get("report_name"),
              meta.get("report_category"), meta.get("prepared_by"),
              path))

    if not args.dry_run:
        con.commit()

    print(f"\nboth fields: {fixed}   one field: {partial}   neither: {failed}")

    if not args.dry_run:
        still, = con.execute(
            "SELECT COUNT(*) FROM documents WHERE country IS NULL").fetchone()
        print(f"still missing country: {still}")

        print("\nin-scope documents by year:")
        for year, c in con.execute("""
            SELECT year, COUNT(*) FROM documents
            WHERE grape_hits > 5 AND year IS NOT NULL
            GROUP BY year ORDER BY year
        """):
            print(f"  {year}  {'#' * c} {c}")


if __name__ == "__main__":
    main()