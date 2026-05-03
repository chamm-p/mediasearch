"""Berechnet Content-Hash + Perceptual-Hash fuer alle Files, denen sie
fehlen, und schreibt sie in die DB. Standalone, ohne LLM.

Aufruf:
    ./run.sh dedupe <root> [--all] [--only image|video]

  --all          alle Files neu hashen (nicht nur die mit fehlenden Hashes)
  --only image   nur Bilder (perceptual hash existiert nur fuer Bilder)
"""
from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path

from PIL import Image

from common import connect, decode_surrogates, init_db


def content_hash(path: Path) -> str:
    """BLAKE2b 128-bit hex (16 bytes -> 32 hex chars)."""
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def perceptual_hash(path: Path) -> int | None:
    """64-bit pHash (DCT-basiert) als signed int."""
    try:
        import imagehash
        with Image.open(path) as im:
            im = im.convert("RGB")
            h = imagehash.phash(im, hash_size=8)
        # 64-bit hash als int (vorzeichenbehaftet fuer SQLite)
        v = int(str(h), 16)
        if v >= (1 << 63):
            v -= (1 << 64)
        return v
    except Exception:
        return None


def cmd_dedupe(args: argparse.Namespace) -> None:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    init_db(root)

    conn = connect(root)
    try:
        sql = "SELECT id, rel_path, type, content_hash, phash_int FROM files"
        wheres: list[str] = []
        params: list = []
        if args.only in ("image", "video"):
            wheres.append("type = ?"); params.append(args.only)
        if not args.all:
            wheres.append("(content_hash IS NULL OR content_hash = '' "
                          "OR (type='image' AND phash_int IS NULL))")
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    total = len(rows)
    print(f"Dedupe-Hashing: {total} Files")
    if not total:
        return

    done = skipped = failed = 0
    t0 = time.time()
    last_print = t0
    BATCH = 50
    pending: list[tuple] = []  # (chash, phash, id)

    def flush(rows_to_write):
        if not rows_to_write: return
        c = connect(root)
        try:
            c.executemany(
                "UPDATE files SET content_hash=?, phash_int=? WHERE id=?",
                rows_to_write,
            )
            c.commit()
        finally:
            c.close()

    for i, r in enumerate(rows, 1):
        fid = r["id"]; rel = r["rel_path"]; kind = r["type"]
        src = root / decode_surrogates(rel)
        if not src.exists():
            skipped += 1
            continue
        chash = content_hash(src)
        phash = perceptual_hash(src) if kind == "image" else None
        if chash:
            pending.append((chash, phash, fid))
            done += 1
        else:
            failed += 1

        if len(pending) >= BATCH:
            flush(pending); pending = []

        if time.time() - last_print >= 2.0 or i == total:
            rate = i / max(time.time() - t0, 0.001)
            eta = (total - i) / rate if rate > 0 else 0
            print(f"[{i}/{total}] done={done} skipped={skipped} "
                  f"failed={failed}  ({rate:.1f}/s, eta {eta/60:.1f}min)",
                  flush=True)
            last_print = time.time()

    flush(pending)
    print(f"Fertig in {time.time()-t0:.1f}s: done={done} skipped={skipped} failed={failed}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Doubletten-Hashing")
    p.add_argument("root", help="Wurzelverzeichnis")
    p.add_argument("--all", action="store_true",
                   help="alle Files neu hashen, nicht nur fehlende")
    p.add_argument("--only", choices=["image", "video"], default=None)
    return p


if __name__ == "__main__":
    cmd_dedupe(build_parser().parse_args())
