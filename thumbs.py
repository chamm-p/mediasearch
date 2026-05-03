"""Thumbnails neu erzeugen ohne LLM-Tagging.

Nutzt die aktuelle THUMB_SIZE/THUMB_QUALITY aus common.py. Ueberschreibt
existierende Thumbs. Reine lokale Operation - kein Netzwerk noetig.

Aufruf:
    ./run.sh thumbs <root> [--only image|video] [--missing-only]
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from common import (
    connect,
    decode_surrogates,
    init_db,
    make_thumb,
    thumb_path,
)


def cmd_thumbs(args: argparse.Namespace) -> None:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    init_db(root)

    conn = connect(root)
    try:
        sql = "SELECT id, rel_path, type FROM files"
        params: list = []
        wheres: list[str] = []
        if args.only in ("image", "video"):
            wheres.append("type = ?")
            params.append(args.only)
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    total = len(rows)
    print(f"Thumbs neu erzeugen: {total} Files")
    done = skipped = failed = 0
    t0 = time.time()
    last = t0
    for i, r in enumerate(rows, 1):
        fid = r["id"]; rel = r["rel_path"]; kind = r["type"]
        dst = thumb_path(root, fid)
        if args.missing_only and dst.exists():
            skipped += 1
            continue
        src = root / decode_surrogates(rel)
        if not src.exists():
            failed += 1
            continue
        ok = make_thumb(src, kind, dst)
        if ok:
            done += 1
        else:
            failed += 1
        if time.time() - last >= 2.0 or i == total:
            rate = i / max(time.time() - t0, 0.001)
            eta = (total - i) / rate if rate > 0 else 0
            print(f"[{i}/{total}] done={done} skipped={skipped} "
                  f"failed={failed}  ({rate:.1f}/s, eta {eta/60:.1f}min)",
                  flush=True)
            last = time.time()
    print(f"Fertig in {time.time()-t0:.1f}s: done={done} "
          f"skipped={skipped} failed={failed}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Thumbnails neu erzeugen")
    p.add_argument("root", help="Wurzelverzeichnis")
    p.add_argument("--only", choices=["image", "video"], default=None)
    p.add_argument("--missing-only", action="store_true",
                   help="nur fehlende Thumbs erzeugen, existierende lassen")
    return p


if __name__ == "__main__":
    cmd_thumbs(build_parser().parse_args())
