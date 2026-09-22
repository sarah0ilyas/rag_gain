import sqlite3
import time

import numpy as np
from sentence_transformers import SentenceTransformer

DB = "manifest.db"
MODEL = "BAAI/bge-small-en-v1.5"
BATCH = 64


def main():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT chunk_id, embed_text FROM chunks ORDER BY chunk_id"
    ).fetchall()
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    texts = [r[1] for r in rows]
    print(f"{len(texts)} chunks to embed with {MODEL}")

    model = SentenceTransformer(MODEL)
    print(f"device: {model.device}, max tokens: {model.max_seq_length}")

    start = time.time()
    vecs = model.encode(
        texts,
        batch_size=BATCH,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
    ).astype(np.float32)
    print(f"embedded in {time.time() - start:.0f}s, shape {vecs.shape}")

    np.save("embeddings.npy", vecs)
    np.save("chunk_ids.npy", ids)

    stored, = con.execute("SELECT COUNT(*) FROM chunks").fetchone()
    norms = np.linalg.norm(vecs, axis=1)
    print(f"\nchunks in table: {stored}, vectors saved: {len(vecs)}")
    if stored != len(vecs):
        print("  MISMATCH between chunks and vectors")
    print(f"vector lengths: min {norms.min():.4f}, max {norms.max():.4f} (should all be 1.0)")
    if np.isnan(vecs).any():
        print("  WARNING: NaN values in embeddings")

    tok = model.tokenizer
    lengths = [len(tok.encode(t)) for t in texts]
    over = sum(l > model.max_seq_length for l in lengths)
    print(f"chunks over the {model.max_seq_length}-token limit: {over}")


if __name__ == "__main__":
    main()
