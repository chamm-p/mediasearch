"""Reparaturversuche fuer Videos, die nicht getaggt werden koennen
(z.B. Fehler 'frame_extract_failed'/'decode_failed' - kaputter Container,
fehlender Index/moov, defekte Frames).

Ablauf pro Video (ffmpeg-Leiter, jeweils in eine Temp-Datei mit GLEICHER
Endung, damit Pfad/DB stabil bleiben):
  1) REMUX  (-c copy)          - repariert Container/Index verlustfrei, schnell
  2) RE-ENCODE (libx264/aac)   - dekodiert tolerant neu, rettet defekte Streams
Danach wird verifiziert, dass aus dem Ergebnis wieder ein Frame lesbar ist.

Sicher per Default (Trockenlauf - meldet nur, was reparierbar WAERE).
Mit --apply werden Originale ersetzt: Original wandert nach
data/<slot>/broken-originals/<pfad>, das reparierte File an den Originalpfad,
der DB-Eintrag wird auf 'pending' gesetzt (damit der Tagger es neu taggt).

Aufruf ueber run.sh:
  ./run.sh fix-videos [root] [--apply] [--limit N] [--no-reencode] [--all]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import common
from common import (connect, db_path, decode_surrogates, extract_video_frame,
                    root_slot, thumb_path, video_duration)

HERE = Path(__file__).resolve().parent


def _root(cli: str | None) -> Path:
    raw = cli
    if not raw:
        sp = HERE / "settings.json"
        if sp.is_file():
            try:
                raw = (json.loads(sp.read_text(encoding="utf-8")).get("root") or "").strip() or None
            except Exception:
                raw = None
    if not raw:
        print("FEHLER: kein Root (settings.json ohne 'root' und kein Argument).")
        sys.exit(1)
    return Path(raw).expanduser().resolve()


def _verify(p: Path) -> bool:
    """Reparatur gilt als gelungen, wenn Dauer > 0 UND ein Frame lesbar ist."""
    if not p.is_file() or p.stat().st_size == 0:
        return False
    dur = video_duration(p)
    ts = dur * 0.25 if dur > 0 else 0.0
    return extract_video_frame(p, ts) is not None


def _run_ffmpeg(cmd: list[str], timeout: int) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def repair(src: Path, out: Path, allow_reencode: bool) -> str | None:
    """Versucht src -> out zu reparieren. Gibt die erfolgreiche Methode
    ('remux'/'reencode') zurueck, sonst None. out hat die Endung von src."""
    common_in = ["ffmpeg", "-y", "-loglevel", "error",
                 "-err_detect", "ignore_err", "-fflags", "+genpts"]
    ladder: list[tuple[str, list[str]]] = [
        ("remux", common_in + ["-i", str(src), "-c", "copy",
                                "-movflags", "+faststart", str(out)]),
    ]
    if allow_reencode:
        ladder.append(("reencode", common_in + [
            "-i", str(src),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "+faststart", str(out)]))
    for method, cmd in ladder:
        if out.exists():
            try: out.unlink()
            except OSError: pass
        ok = _run_ffmpeg(cmd, timeout=1800)
        if ok and _verify(out):
            return method
    if out.exists():
        try: out.unlink()
        except OSError: pass
    return None


def _candidates(root: Path, all_videos: bool, limit: int | None):
    conn = connect(root)
    try:
        if all_videos:
            sql = "SELECT id, rel_path, error FROM files WHERE type='video'"
        else:
            sql = ("SELECT id, rel_path, error FROM files "
                   "WHERE type='video' AND status='error'")
        sql += " ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _apply(root: Path, fid: int, rel: str, src_abs: Path, repaired: Path) -> None:
    """Original wegsichern, repariertes File an Originalpfad, DB -> pending."""
    broken_dir = root_slot(root) / "broken-originals"
    backup = broken_dir / decode_surrogates(rel)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_abs), str(backup))
    shutil.move(str(repaired), str(src_abs))
    st = src_abs.stat()
    conn = connect(root)
    try:
        conn.execute(
            "UPDATE files SET status='pending', error='', tagged_at=0, "
            "started_at=0, size=?, mtime=? WHERE id=?",
            (st.st_size, st.st_mtime, fid))
        conn.commit()
    finally:
        conn.close()
    try:
        thumb_path(root, fid).unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Videos per ffmpeg reparieren")
    ap.add_argument("root", nargs="?", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="Originale wirklich ersetzen (sonst nur Trockenlauf)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-reencode", action="store_true",
                    help="nur Remux versuchen, kein (verlustbehaftetes) Re-Encode")
    ap.add_argument("--all", action="store_true",
                    help="ALLE Videos probieren, nicht nur status=error")
    args = ap.parse_args()

    root = _root(args.root)
    rows = _candidates(root, args.all, args.limit)
    mode = "APPLY (ersetzt Originale)" if args.apply else "Trockenlauf (keine Aenderung)"
    print(f"Root: {root}")
    print(f"Kandidaten: {len(rows)} Video(s)  |  Modus: {mode}")
    if not rows:
        print("Nichts zu tun.")
        return

    fixable = fixed = failed = 0
    tmpdir = Path(tempfile.mkdtemp(prefix="ms-fixvid-"))
    try:
        for i, r in enumerate(rows, 1):
            fid, rel = r["id"], r["rel_path"]
            src = (root / decode_surrogates(rel)).resolve()
            label = f"[{i}/{len(rows)}] {rel}"
            if not src.is_file():
                print(f"{label}: Datei fehlt - uebersprungen")
                continue
            out = tmpdir / ("fix" + (src.suffix or ".mp4"))
            method = repair(src, out, allow_reencode=not args.no_reencode)
            if method is None:
                failed += 1
                print(f"{label}: NICHT reparierbar")
                continue
            fixable += 1
            if args.apply:
                try:
                    _apply(root, fid, rel, src, out)
                    fixed += 1
                    print(f"{label}: repariert ({method}) -> ersetzt, DB=pending")
                except Exception as e:
                    print(f"{label}: repariert ({method}), aber Ersetzen FEHLGESCHLAGEN: {e}")
            else:
                print(f"{label}: reparierbar ({method})")
                if out.exists():
                    try: out.unlink()
                    except OSError: pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "-" * 55)
    if args.apply:
        print(f"Fertig: {fixed} repariert+ersetzt, {failed} nicht reparierbar.")
        print("Originale gesichert unter data/<slot>/broken-originals/.")
        if fixed:
            print("-> Jetzt taggen:  ./run.sh tag  (die reparierten sind 'pending').")
    else:
        print(f"Trockenlauf: {fixable} reparierbar, {failed} nicht reparierbar.")
        if fixable:
            print("-> Zum Anwenden:  ./run.sh fix-videos --apply")


if __name__ == "__main__":
    main()
