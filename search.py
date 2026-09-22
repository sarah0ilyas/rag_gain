"""
Ask a question against the embedded chunks.

Filters are applied BEFORE ranking by similarity. That matters: embeddings
are good at topic and weak at which fruit. "Weather hit apple production"
and "weather hit grape production" look almost the same to the model, so
without a fruit filter a grape question regularly pulls in apple text.

Usage:
    python search.py "How did drought affect Chilean grape exports?"
    python search.py "How did drought affect Chilean grape exports?" --commodity grapes
    python search.py "..." --commodity grapes --country Chile --k 8
    python search.py "..." --commodity grapes --confirmed    # skip 'inherited' chunks
"""

import argparse
import sqlite3

import numpy as np
from sentence_transformers import SentenceTransformer

DB = "manifest.db"
MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--commodity", help="grapes, raisins, apples, pears, general")
    ap.add_argument("--country")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--confirmed", action="store_true",
                    help="leave out chunks labelled by position only")
    args = ap.parse_args()

    vecs = np.load("embeddings.npy")
    ids = np.load("chunk_ids.npy")

    con = sqlite3.connect(DB)
    meta = {r[0]: r[1:] for r in con.execute("""
        SELECT chunk_id, country, year, commodity, section, label_basis,
               report_number, text
        FROM chunks""")}

    # --- filter first
    keep = np.ones(len(ids), dtype=bool)
    for i, cid in enumerate(ids):
        country, year, commodity, section, basis, rn, text = meta[int(cid)]
        if args.commodity and commodity != args.commodity:
            keep[i] = False
        if args.country and (country or "").lower() != args.country.lower():
            keep[i] = False
        if args.confirmed and basis == "inherited":
            keep[i] = False

    if not keep.any():
        print("no chunks match those filters")
        return

    # --- then rank by meaning
    model = SentenceTransformer(MODEL)
    q = model.encode(QUERY_PREFIX + args.question, normalize_embeddings=True)

    scores = vecs @ q
    scores[~keep] = -np.inf
    top = np.argsort(-scores)[:args.k]

    print(f"\n{args.question}")
    print(f"searched {keep.sum()} of {len(ids)} chunks\n")
    for rank, i in enumerate(top, 1):
        country, year, commodity, section, basis, rn, text = meta[int(ids[i])]
        print(f"{rank}. {scores[i]:.3f}  {country} {year}  {commodity}/{section}"
              f"  [{basis}]  {rn or ''}")
        print(f"   {text[:240]}...")
        print()


if __name__ == "__main__":
    main()