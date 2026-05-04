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
import io
import time
from pathlib import Path

from PIL import Image

from common import (connect, decode_surrogates, extract_video_frame,
                    init_db, thumb_path, video_duration)


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


def _phash_pil(im: Image.Image) -> int | None:
    try:
        import imagehash
        h = imagehash.phash(im, hash_size=8)
        v = int(str(h), 16)
        if v >= (1 << 63):
            v -= (1 << 64)
        return v
    except Exception:
        return None


def perceptual_hash_image(path: Path, root: Path | None = None,
                          file_id: int | None = None) -> int | None:
    """pHash auf Bild. Schneller Pfad: existierendes 240x240-Thumbnail nutzen
    (gleicher pHash wie Original, ~30x schneller einzulesen). Fallback Original."""
    if root is not None and file_id is not None:
        tp = thumb_path(root, file_id)
        if tp.exists():
            try:
                with Image.open(tp) as im:
                    im = im.convert("RGB")
                    v = _phash_pil(im)
                    if v is not None:
                        return v
            except Exception:
                pass
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            return _phash_pil(im)
    except Exception:
        return None


def perceptual_hash_video(path: Path, root: Path, file_id: int) -> int | None:
    """pHash auf mittlerem Frame des Videos.
    Schneller Pfad: existierendes Thumbnail (bereits Mittel-Frame) verwenden.
    Fallback: ffmpeg neu starten und Frame extrahieren."""
    # 1) Thumbnail existiert? Bereits Mittel-Frame des Videos -> ~300x schneller
    tp = thumb_path(root, file_id)
    if tp.exists():
        try:
            with Image.open(tp) as im:
                im = im.convert("RGB")
                v = _phash_pil(im)
                if v is not None:
                    return v
        except Exception:
            pass
    # 2) Fallback: Frame frisch extrahieren
    dur = video_duration(path)
    ts = dur / 2 if dur > 0 else 1.0
    data = extract_video_frame(path, ts)
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            return _phash_pil(im)
    except Exception:
        return None


def perceptual_hash(path: Path, kind: str,
                    root: Path | None = None,
                    file_id: int | None = None) -> int | None:
    if kind == "image":
        return perceptual_hash_image(path, root, file_id)
    if kind == "video" and root is not None and file_id is not None:
        return perceptual_hash_video(path, root, file_id)
    return None


def cmd_dedupe(args: argparse.Namespace) -> None:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    init_db(root)

    # EINE Connection fuer den ganzen Run - vermeidet WAL-Bloat durch
    # tausende Open/Close-Zyklen.
    conn = connect(root)
    try:
        sql = "SELECT id, rel_path, type, content_hash, phash_int FROM files"
        wheres: list[str] = []
        params: list = []
        if args.only in ("image", "video"):
            wheres.append("type = ?"); params.append(args.only)
        if not args.all:
            wheres.append("(content_hash IS NULL OR content_hash = '' "
                          "OR phash_int IS NULL)")
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()

        total = len(rows)
        print(f"Dedupe-Hashing: {total} Files")
        if not total:
            return

        done = skipped = failed = 0
        t0 = time.time()
        last_print = t0
        last_checkpoint = t0
        BATCH = 50
        pending: list[tuple] = []

        def flush():
            if not pending:
                return
            conn.executemany(
                "UPDATE files SET content_hash=?, phash_int=? WHERE id=?",
                pending,
            )
            conn.commit()
            pending.clear()

        for i, r in enumerate(rows, 1):
            fid = r["id"]; rel = r["rel_path"]; kind = r["type"]
            src = root / decode_surrogates(rel)
            if not src.exists():
                skipped += 1
                continue
            existing_chash = (r["content_hash"] or "") if not args.all else ""
            existing_phash = r["phash_int"]      if not args.all else None
            chash = existing_chash if existing_chash else content_hash(src)
            if existing_phash is not None:
                phash = existing_phash
            else:
                phash = perceptual_hash(src, kind, root=root, file_id=fid)
            if chash:
                pending.append((chash, phash, fid))
                done += 1
            else:
                failed += 1

            if len(pending) >= BATCH:
                flush()

            now = time.time()
            # Alle ~30s WAL-Checkpoint, damit die WAL-Datei nicht waechst
            # und nachfolgende Reads/Writes schnell bleiben.
            if now - last_checkpoint >= 30:
                # TRUNCATE schreibt zurueck UND truncated die WAL-Datei -
                # haelt sie damit wirklich klein. Wir haben keine
                # parallelen Reader, also unkritisch.
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                last_checkpoint = now
            if now - last_print >= 2.0 or i == total:
                rate = i / max(now - t0, 0.001)
                eta = (total - i) / rate if rate > 0 else 0
                print(f"[{i}/{total}] done={done} skipped={skipped} "
                      f"failed={failed}  ({rate:.1f}/s, eta {eta/60:.1f}min)",
                      flush=True)
                last_print = now

        flush()
        # finaler Checkpoint - WAL leer machen
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(f"Fertig in {time.time()-t0:.1f}s: done={done} skipped={skipped} failed={failed}")
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Doubletten-Hashing")
    p.add_argument("root", help="Wurzelverzeichnis")
    p.add_argument("--all", action="store_true",
                   help="alle Files neu hashen, nicht nur fehlende")
    p.add_argument("--only", choices=["image", "video"], default=None)
    return p


if __name__ == "__main__":
    cmd_dedupe(build_parser().parse_args())
