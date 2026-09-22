"""
Split every in-scope document into retrievable chunks, tagged with the
commodity and section they came from.

Why section-aware rather than fixed-size: a Fresh Deciduous Fruit Annual
holds apples, pears and table grapes in separate sections. A fixed 800-token
cut produces chunks that are half apples, half grapes, and a grape query
then retrieves apple text. Cutting on section boundaries prevents that.

How sections are found (from inspecting the corpus):

  - Every commodity section is headed by a standalone PSD commodity name:
        "Apples, Fresh"   "Pears, Fresh"   "Grapes, Table, Fresh"
    Pre-2019 reports add a "Commodities:" line above it; recent ones don't.
    Keying on the name line covers both templates.

  - The name repeats inside its own section (trade-table and PSD-table
    headings). So a boundary is a CHANGE of commodity, not any mention.

  - Order varies by country (SA: apples, pears, grapes; Chile 2019:
    apples, grapes, pears), so nothing assumes an order.

  - Text before the first commodity heading (Executive Summary, Report
    Highlights) is tagged "general".

  - Single-commodity reports (raisin annuals, grape spotlights) have no
    commodity headings; their commodity comes from the report name.

Tables and charts extract as columns of bare numbers. They are dropped in
this version rather than half-parsed - a chunk of loose digits matches
numeric queries and answers them wrongly.

Usage:
    python chunk.py --doc 2024-11-south-africa    # preview one document
    python chunk.py                               # chunk everything
"""

import argparse
import re
import sqlite3
from collections import Counter
from collections import Counter

DB = "manifest.db"

TARGET_WORDS = 350          # roughly 450-500 tokens
OVERLAP_SENTENCES = 1
MIN_CHUNK_WORDS = 40        # fold a small trailing chunk into its neighbour
MIN_KEEP_WORDS = 25         # merge any chunk smaller than this
MIN_FINAL_WORDS = 8         # a chunk still this small after merging is debris


# ---------------------------------------------------------------- headings

# A commodity heading: a whole line that is a fresh-fruit PSD name.
COMMODITY_RE = re.compile(
    r"^(?:(?:apples?|pears?|grapes?)\b[^.]{0,25}\bfresh\b.*"
    r"|table\s+grapes?(?:\s*,\s*fresh)?)$",
    re.I,
)

# Subsection headings. Colon optional - old template has it, new doesn't.
SECTION_RE = re.compile(
    r"^(production|consumption|trade|exports?|imports?|policy|"
    r"stocks|marketing|prices?|executive summary|report highlights)\s*:?$",
    re.I,
)


# Canada writes headings in capitals with no trailing "Fresh":
#   APPLES   PEARS   FRESH TABLE GRAPES   GRAPES
# Limited to all-caps on purpose - a title-case "Apples" alone on a line
# is more often a chart legend than a heading.
CAPS_RE = re.compile(r"^(FRESH\s+)?(TABLE\s+)?(APPLES?|PEARS?|GRAPES?)$")

# China (and others) use a bare plural in title case: "Apples", "Pears",
# "Grapes". Kept exact and plural: the same reports use singular or
# lowercase fruit names ("Apple", "grape") as chart legends.
TITLE_RE = re.compile(r"^(Apples|Pears|Grapes)$")


def commodity_key(line):
    """Normalise a heading to apples / pears / grapes, or None."""
    s = line.strip()
    if " MY" in s or "–" in s or "-" in s:
        return None              # units legend, e.g. "Table Grapes MY – Oct..."
    if not (COMMODITY_RE.match(s) or CAPS_RE.match(s) or TITLE_RE.match(s)):
        return None
    low = s.lower()
    if "grape" in low:
        return "grapes"
    if "apple" in low:
        return "apples"
    if "pear" in low:
        return "pears"
    return None


def section_key(line):
    m = SECTION_RE.match(line.strip())
    if not m:
        return None
    name = m.group(1).lower()
    if name in ("executive summary", "report highlights"):
        return "summary"
    if name in ("export", "exports", "import", "imports"):
        return "trade"
    if name in ("price", "prices"):
        return "prices"
    return name


def commodity_from_name(report_name):
    """Commodity for a report with no commodity headings."""
    n = (report_name or "").lower()
    if "raisin" in n:
        return "raisins"
    if "grape" in n:
        return "grapes"
    return "general"


# ---------------------------------------------------------------- cleaning

BOILERPLATE = re.compile(
    r"^(THIS REPORT CONTAINS|STATEMENTS OF OFFICIAL|USDA STAFF AND NOT|"
    r"Required Report|Voluntary Report|Report Number|Report Name|"
    r"Country:|Post:|Report Category|Prepared By|Approved By|Date:|"
    r"Source:|Sources:|GAIN Report Number|Attachments:|"
    r"OFFICIAL DATA CAN BE ACCESSED|No Attachments)",
    re.I,
)

# PSD table row and column labels. Long enough to pass the length tests but
# never prose. Limited to short lines without a full stop so a sentence such
# as "Exports to China rose..." is untouched.
PSD_LABEL = re.compile(
    r"\((HA|MT|MMT|1000 MT|TREES|1000 TREES|MT/HA)\)"
    r"|^(Market\s+(Year\s+)?Begins?|USDA Official|New Post|Not Official|"
    r"Official|Area\s+(Planted|Harvested)|(Non-)?Bearing Trees|Total Trees|"
    r"Total\s+(Supply|Distribution)|Commercial Production|"
    r"Non-Comm\.?\s+Production|Fresh Dom\.?\s+Consumption|"
    r"Withdrawal From Market|For Processing|Ending Stocks|Beginning Stocks)\b",
    re.I,
)

# Figure and table captions. The chart or table they describe is dropped,
# so the caption alone describes nothing and would become a stub chunk.
CAPTION = re.compile(r"^(Figure|Table|Graph|Chart|Map)\s*\d+\s*[:.]", re.I)


def is_noise(line):
    """True for lines that carry no prose: page numbers, table cells,
    chart axis labels, boilerplate."""
    s = line.strip()
    if not s:
        return True
    if BOILERPLATE.match(s) or CAPTION.match(s):
        return True
    if len(s) < 50 and not s.endswith(".") and PSD_LABEL.search(s):
        return True                          # "Area Planted (HA)", "USDA Official"
    letters = sum(c.isalpha() for c in s)
    if letters == 0:
        return True                          # "35,029", "-", "2"
    if len(s) < 25 and letters / len(s) < 0.5:
        return True                          # "MY2011/12", "(HA) ,(MT)"
    if len(s) < 18 and not s.endswith((".", ":")) and letters < 12:
        return True                          # stray cell text: "Jan", "Area"
    return False


# ---------------------------------------------------------------- splitting

LOOKAHEAD = 25


def followed_by_content(lines, i, repeated):
    """A list item runs straight into the next fruit; a real heading does
    not, even if a table or chart sits between it and its prose. The EU
    reports open with
    "This report covers: Apples, Fresh / Pears, Fresh / Table Grapes, Fresh"
    and an author list "Apples / Sabine Lieberz / Pears / ...". Those are
    followed by another short label or the next fruit, so they are rejected.
    """
    examined = 0
    for nxt in lines[i + 1:]:
        s = nxt.strip()
        if not s or s in repeated:
            continue
        # Headings are checked before noise, as in the main loop: short
        # headings like "Table Grapes" also pass the noise test, and
        # skipping them would step straight over the next fruit.
        if commodity_key(s):
            return False                 # straight into another fruit: a list
        if section_key(s):
            return True                  # "Production:" - a real heading
        if len(s) >= 60 or s.endswith("."):
            return True                  # prose
        # Short labels (a PSD table's "2020/2021", "USDA Official", a chart
        # title) don't decide anything; keep looking.
        examined += 1
        if examined >= LOOKAHEAD:
            break
    # No other fruit within the window. Lists hit their next item within a
    # line or two, so a heading that gets this far is a real one.
    return True


def split_sections(text, fallback_commodity):
    """Walk the document line by line, yielding
    (commodity, section, [lines]) spans."""
    commodity = None
    section = "summary"
    seen = {"summary"}          # sections already visited in this commodity
    buf = []
    spans = []
    saw_heading = False

    def flush():
        if buf:
            spans.append((commodity or "general", section, list(buf)))
            buf.clear()

    lines = text.split("\n")

    # Running headers repeat on every page ("EU Fresh Deciduous Fruit Annual
    # Report 2022"). Any short line seen five or more times is page furniture.
    counts = Counter(l.strip() for l in lines)
    repeated = {k for k, v in counts.items() if v >= 5 and 0 < len(k) < 90}

    for i, raw in enumerate(lines):
        line = raw.strip()

        ck = commodity_key(line)
        if ck and not followed_by_content(lines, i, repeated):
            continue                         # a list item, not a heading
        if ck:
            saw_heading = True
            if ck != commodity:              # a change, not a repeat
                flush()
                commodity = ck
                section = "overview"
                seen = {"overview"}
            continue

        sk = section_key(line)
        if sk:
            # Each subsection appears once per commodity. A heading for one
            # already visited is a chart axis label ("Production" on a yield
            # chart inside the consumption section), not a real heading.
            if sk != section and sk not in seen:
                flush()
                section = sk
                seen.add(sk)
            continue

        if line.lower().rstrip(":") == "commodities":
            continue

        if line not in repeated and not is_noise(line):
            buf.append(line)

    flush()

    # No commodity headings anywhere: single-commodity report.
    if not saw_heading:
        spans = [(fallback_commodity, s, l) for _, s, l in spans]
    # Title names one commodity (raisin annual, grape report) but a stray
    # heading appeared - a PSD table for fresh grapes, say. Text that would
    # otherwise be "general" belongs to the title's commodity.
    elif fallback_commodity != "general":
        spans = [(fallback_commodity if c == "general" else c, s, l)
                 for c, s, l in spans]

    return spans


SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def pack(lines):
    """Join wrapped PDF lines into prose, then pack sentences into chunks
    of about TARGET_WORDS with a one-sentence overlap."""
    prose = " ".join(lines)
    prose = re.sub(r"\s+", " ", prose).strip()
    prose = re.sub(r"(\w)- (\w)", r"\1\2", prose)   # rejoin hyphenated breaks
    if not prose:
        return []

    sentences = SENT_RE.split(prose)
    chunks, cur, cur_words = [], [], 0

    for s in sentences:
        w = len(s.split())
        if cur and cur_words + w > TARGET_WORDS:
            chunks.append(" ".join(cur))
            cur = cur[-OVERLAP_SENTENCES:] if OVERLAP_SENTENCES else []
            cur_words = sum(len(x.split()) for x in cur)
        cur.append(s)
        cur_words += w

    if cur:
        chunks.append(" ".join(cur))

    # fold a tiny trailing chunk into the previous one
    if len(chunks) > 1 and len(chunks[-1].split()) < MIN_CHUNK_WORDS:
        chunks[-2] = chunks[-2] + " " + chunks[-1]
        chunks.pop()

    return chunks


def header(doc, commodity, section):
    """Prepended to the text that gets embedded, so a paragraph that never
    names its country or fruit still carries that context."""
    return (f"{doc['country']} | {doc['year']} | {doc['report_name']} | "
            f"{commodity} | {section}")


# ---------------------------------------------------------------- main

def init(con):
    con.execute("DROP TABLE IF EXISTS chunks")
    con.execute("""
        CREATE TABLE chunks (
            chunk_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            local_path    TEXT,
            report_number TEXT,
            country       TEXT,
            year          INTEGER,
            report_name   TEXT,
            commodity     TEXT,
            label_basis   TEXT,
            section       TEXT,
            chunk_index   INTEGER,
            n_words       INTEGER,
            text          TEXT,
            embed_text    TEXT
        )
    """)
    con.execute("CREATE INDEX idx_chunk_commodity ON chunks(commodity)")
    con.execute("CREATE INDEX idx_chunk_country ON chunks(country)")
    con.commit()


def load_docs(con, pattern=None, min_hits=5):
    q = """SELECT local_path, report_number, country, year, report_name, text
           FROM documents WHERE grape_hits > ?"""
    args = [min_hits]
    if pattern:
        q += " AND local_path LIKE ?"
        args.append(f"%{pattern}%")
    cols = ["local_path", "report_number", "country", "year",
            "report_name", "text"]
    return [dict(zip(cols, r)) for r in con.execute(q, args)]


def merge_small(pieces):
    """Fold chunks under MIN_KEEP_WORDS into a neighbour with the same
    commodity. Merging rather than dropping keeps short real content -
    a one-sentence policy note - while removing fragment chunks.
    The larger chunk's section label wins."""
    out = []
    for p in pieces:
        small = len(p["text"].split()) < MIN_KEEP_WORDS
        if small and out and out[-1]["commodity"] == p["commodity"]:
            out[-1]["text"] += " " + p["text"]
        else:
            out.append(dict(p))

    # a small chunk at the very start of a commodity run: fold forward
    i = 0
    while i < len(out) - 1:
        if (len(out[i]["text"].split()) < MIN_KEEP_WORDS
                and out[i]["commodity"] == out[i + 1]["commodity"]):
            out[i + 1]["text"] = out[i]["text"] + " " + out[i + 1]["text"]
            out.pop(i)
        else:
            i += 1
    return out


FRUIT_RE = {
    "apples":  re.compile(r"\bapples?\b", re.I),
    "pears":   re.compile(r"\bpears?\b", re.I),
    "grapes":  re.compile(r"\bgrapes?\b|\bvineyards?\b|\bviticulture\b", re.I),
    "raisins": re.compile(r"\braisins?\b|\bsultanas?\b", re.I),
}


def check_label(commodity, text):
    """Second opinion on a structural label, from what the chunk says.

    Returns (commodity, basis):
      heading   - the chunk names the fruit its section heading gave it
      content   - relabelled: it never names its own fruit but clearly
                  names another (a missed heading upstream)
      inherited - names no fruit at all; label kept from position only.
                  Could be a grape paragraph that doesn't repeat the word,
                  or a trailing chapter (EU pesticide rules) that followed
                  the last grape section. Retrieval can weight these lower.
      title     - raisin/grape report, commodity from the report name
      none      - general text
    """
    counts = {k: len(r.findall(text)) for k, r in FRUIT_RE.items()}
    best = max(counts, key=counts.get)

    if commodity in ("apples", "pears", "grapes", "raisins"):
        if counts[commodity] > 0:
            return commodity, "heading"
        if counts[best] >= 2:
            return best, "content"
        return commodity, "inherited"

    # general: relabel only when one fruit clearly dominates
    others = sum(v for k, v in counts.items() if k != best)
    if counts[best] >= 3 and others == 0:
        return best, "content"
    return commodity, "none"


def chunk_doc(doc):
    fallback = commodity_from_name(doc["report_name"])
    pieces = []
    for commodity, section, lines in split_sections(doc["text"], fallback):
        for body in pack(lines):
            pieces.append({"commodity": commodity, "section": section,
                           "text": body})

    # Anything still tiny after merging is end-of-report debris ("Non-Comm.
    # Fresh Dom. Withdrawal From") with no neighbour of the same fruit.
    kept = [p for p in merge_small(pieces)
            if len(p["text"].split()) >= MIN_FINAL_WORDS]

    out = []
    for idx, p in enumerate(kept):
        commodity, basis = check_label(p["commodity"], p["text"])
        if basis == "heading" and fallback in ("raisins", "grapes") \
                and commodity == fallback:
            basis = "title"
        out.append({
            "commodity": commodity,
            "label_basis": basis,
            "section": p["section"],
            "chunk_index": idx,
            "n_words": len(p["text"].split()),
            "text": p["text"],
            "embed_text": header(doc, commodity, p["section"])
                          + "\n\n" + p["text"],
        })
    return out


def preview(con, pattern):
    docs = load_docs(con, pattern, min_hits=-1)
    if not docs:
        print(f"no document matches '{pattern}'")
        return
    doc = docs[0]
    chunks = chunk_doc(doc)
    print(f"{doc['local_path']}\n{len(chunks)} chunks\n")
    for c in chunks:
        print(f"--- [{c['chunk_index']}] {c['commodity']} / {c['section']} "
              f"[{c['label_basis']}] ({c['n_words']} words)")
        print(c["text"][:300] + ("..." if len(c["text"]) > 300 else ""))
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", help="preview chunks for one matching document")
    ap.add_argument("--min-hits", type=int, default=5,
                    help="grape_hits threshold for inclusion")
    args = ap.parse_args()

    con = sqlite3.connect(DB)

    if args.doc:
        preview(con, args.doc)
        return

    init(con)
    docs = load_docs(con, min_hits=args.min_hits)
    print(f"chunking {len(docs)} documents")

    rows, empty = [], []
    for doc in docs:
        chunks = chunk_doc(doc)
        if not chunks:
            empty.append(doc["local_path"])
        for c in chunks:
            rows.append((doc["local_path"], doc["report_number"],
                         doc["country"], doc["year"], doc["report_name"],
                         c["commodity"], c["label_basis"], c["section"],
                         c["chunk_index"], c["n_words"], c["text"],
                         c["embed_text"]))

    con.executemany("""
        INSERT INTO chunks (local_path, report_number, country, year,
            report_name, commodity, label_basis, section, chunk_index,
            n_words, text, embed_text)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    con.commit()

    # --- verify, don't assume
    stored, = con.execute("SELECT COUNT(*) FROM chunks").fetchone()
    covered, = con.execute(
        "SELECT COUNT(DISTINCT local_path) FROM chunks").fetchone()
    print(f"\nbuilt {len(rows)}, stored {stored}")
    print(f"documents with chunks: {covered} of {len(docs)}")
    if stored != len(rows):
        print("  MISMATCH between built and stored")
    if empty:
        print(f"  {len(empty)} documents produced no chunks:")
        for p in empty[:10]:
            print(f"    {p}")

    print("\nchunks by commodity:")
    for k, n in con.execute(
            "SELECT commodity, COUNT(*) FROM chunks GROUP BY commodity "
            "ORDER BY COUNT(*) DESC"):
        print(f"  {n:6d}  {k}")

    print("\nlabel basis by commodity:")
    for k, b, n in con.execute(
            "SELECT commodity, label_basis, COUNT(*) FROM chunks "
            "GROUP BY commodity, label_basis ORDER BY commodity, COUNT(*) DESC"):
        print(f"  {k:8s} {b:10s} {n:6d}")

    print("\nchunks by section:")
    for k, n in con.execute(
            "SELECT section, COUNT(*) FROM chunks GROUP BY section "
            "ORDER BY COUNT(*) DESC"):
        print(f"  {n:6d}  {k}")

    sizes = [r[0] for r in con.execute("SELECT n_words FROM chunks")]
    if sizes:
        sizes.sort()
        pct = lambda p: sizes[int(p * (len(sizes) - 1))]
        print(f"\nchunk size (words): min {sizes[0]}, p10 {pct(.1)}, "
              f"median {pct(.5)}, p90 {pct(.9)}, max {sizes[-1]}")


if __name__ == "__main__":
    main()