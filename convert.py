"""Videos verlustarm komprimieren (H.265/HEVC bzw. AV1), mit technischer QS
und Platz-Ueberwachung. Rekursiv ab dem in mediasearch gesetzten Root.

Ablauf pro Video:
  1) ffprobe: Soll-Werte des Originals (Dauer, Audio, Aufloesung, Codec)
  2) Encode nach <datei>.msconv.<ext> (temporaer, gleiches Verzeichnis)
  3) QS gegen die Soll-Werte - JEDE Pruefung ist ein Abbruchkriterium
  4) Original -> Papierkorb IM Medien-Root (reines Umbenennen, kein Platz!)
     bzw. mit --delete direkt loeschen
  5) Konvertierte Datei an den Originalpfad
  6) DB: size/mtime aktualisieren, Tags BEHALTEN (Bildinhalt ist ja gleich)

Platz-Ueberwachung: faellt der freie Platz unter die Reserve, haelt der Lauf
an und sagt dir, was zu tun ist (Papierkorb pruefen+leeren, dann weiter).

Aufruf:
  ./run.sh convert                      # Trockenlauf: was waere zu tun?
  ./run.sh convert --apply              # wirklich konvertieren
  ./run.sh convert --apply --delete     # Originale direkt loeschen
  ./run.sh convert --apply --codec av1  # AV1 statt H.265
  ./run.sh convert --empty-trash        # Papierkorb endgueltig loeschen
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import common
from common import connect, db_path, decode_surrogates

HERE = Path(__file__).resolve().parent
TRASH_NAME = ".mediasearch-converted-originals"   # auch in common.EXCLUDE_DIRS!
TMP_MARK = ".msconv"


# ---------- Root ----------

def resolve_root(cli: str | None) -> Path:
    raw = cli
    if not raw:
        sp = HERE / "settings.json"
        if sp.is_file():
            try:
                raw = (json.loads(sp.read_text(encoding="utf-8")).get("root") or "").strip() or None
            except Exception:
                raw = None
    if not raw:
        print("FEHLER: kein Wurzelverzeichnis (settings.json ohne 'root', kein Argument).")
        sys.exit(1)
    p = Path(raw).expanduser().resolve()
    if not p.is_dir():
        print(f"FEHLER: Wurzelverzeichnis nicht erreichbar: {p}")
        sys.exit(1)
    return p


# ---------- ffprobe / Medien-Infos ----------

def probe(path: Path) -> dict | None:
    """Technische Kennwerte via ffprobe. None, wenn nicht lesbar."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout or "{}")
    except Exception:
        return None
    vs = [s for s in d.get("streams", []) if s.get("codec_type") == "video"]
    aus = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
    if not vs:
        return None
    v = vs[0]
    dur = 0.0
    for src in (d.get("format", {}).get("duration"), v.get("duration")):
        try:
            dur = float(src)
            if dur > 0:
                break
        except (TypeError, ValueError):
            continue
    return {
        "duration": dur,
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "vcodec": v.get("codec_name") or "",
        "n_audio": len(aus),
        "audio_channels": sum(int(a.get("channels") or 0) for a in aus),
        "size": path.stat().st_size,
    }


def decodes_ok(path: Path) -> bool:
    """Prueft, dass echt dekodiert werden kann (nicht nur Header lesbar)."""
    return common.extract_video_frame(path, 0.0) is not None


# ---------- Encoder ----------

def build_encode_cmd(src: Path, dst: Path, codec: str, crf: int,
                     preset: str) -> list[str]:
    """ffmpeg-Kommando. -map 0 + -c:a copy: ALLE Streams und Audio
    unveraendert uebernehmen (kein Qualitaetsverlust beim Ton, schneller)."""
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
           "-i", str(src), "-map", "0", "-map_metadata", "0"]
    if codec == "av1":
        cmd += ["-c:v", "libsvtav1", "-crf", str(crf), "-preset", preset,
                "-pix_fmt", "yuv420p10le", "-svtav1-params", "tune=0"]
    else:  # hevc
        cmd += ["-c:v", "libx265", "-crf", str(crf), "-preset", preset,
                "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
    # Audio/Untertitel unveraendert; Daten-Streams verwerfen (machen oft Aerger)
    cmd += ["-c:a", "copy", "-c:s", "copy", "-dn",
            "-movflags", "+faststart", str(dst)]
    return cmd


# ---------- QS ----------

def qs_check(orig: dict, new_path: Path, tol_sec: float) -> tuple[bool, str]:
    """Technische Qualitaetssicherung VOR dem Loeschen des Originals.
    Jede Pruefung ist ein Abbruchkriterium."""
    new = probe(new_path)
    if new is None:
        return False, "nicht lesbar / kein Video-Stream"
    if not decodes_ok(new_path):
        return False, "kein Frame dekodierbar"
    if orig["duration"] > 0:
        diff = abs(new["duration"] - orig["duration"])
        if diff > tol_sec:
            return False, (f"Laenge weicht ab: {new['duration']:.2f}s vs "
                           f"{orig['duration']:.2f}s (Delta {diff:.2f}s)")
    elif new["duration"] <= 0:
        return False, "Dauer 0"
    if orig["n_audio"] > 0 and new["n_audio"] == 0:
        return False, "Audio fehlt (Original hatte Ton)"
    if orig["audio_channels"] > 0 and new["audio_channels"] < orig["audio_channels"]:
        return False, (f"Audio-Kanaele reduziert: {new['audio_channels']} "
                       f"statt {orig['audio_channels']}")
    if (new["width"], new["height"]) != (orig["width"], orig["height"]):
        return False, (f"Aufloesung weicht ab: {new['width']}x{new['height']} "
                       f"statt {orig['width']}x{orig['height']}")
    if new["size"] >= orig["size"]:
        return False, (f"nicht kleiner ({_mb(new['size'])} vs "
                       f"{_mb(orig['size'])}) - Konvertierung sinnlos")
    return True, "ok"


def _mb(n: int) -> str:
    return f"{n/1e6:.1f}MB"


# ---------- Platz ----------

def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def trash_size(trash: Path) -> int:
    if not trash.is_dir():
        return 0
    tot = 0
    for p in trash.rglob("*"):
        try:
            if p.is_file():
                tot += p.stat().st_size
        except OSError:
            pass
    return tot


# ---------- DB ----------

def db_update_converted(root: Path, rel: str, new_abs: Path) -> str:
    """size/mtime aktualisieren, Tags BEHALTEN. Wichtig: ein normaler
    Re-Scan wuerde die Datei als 'geaendert' sehen und auf pending setzen
    -> alle Tags neu per LLM. Der Bildinhalt ist aber identisch, deshalb
    schreiben wir die neuen Werte direkt und lassen status/tags in Ruhe."""
    st = new_abs.stat()
    conn = connect(root)
    try:
        row = conn.execute("SELECT id, status FROM files WHERE rel_path=?",
                           (rel,)).fetchone()
        if row is None:
            return "nicht in DB"
        conn.execute(
            "UPDATE files SET size=?, mtime=?, content_hash='', phash_int=NULL "
            "WHERE id=?", (st.st_size, st.st_mtime, row["id"]))
        conn.commit()
        return "aktualisiert"
    finally:
        conn.close()


# ---------- Kandidaten ----------

def candidates(root: Path, skip_codecs: set[str] | None,
               min_mb: float, limit: int | None) -> list[Path]:
    """Alle Videos unter root (rekursiv), ohne Papierkorb und Temp-Reste.
    skip_codecs: bereits effizient kodierte Videos AUSSCHLIESSEN (z.B. hevc/av1
    nicht nochmal durch den Encoder schicken - das kostet nur Qualitaet)."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d != TRASH_NAME and d not in common.EXCLUDE_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if common.media_type(p) != "video":
                continue
            if TMP_MARK in p.name:
                continue
            try:
                if p.stat().st_size < min_mb * 1e6:
                    continue
            except OSError:
                continue
            out.append(p)
    out.sort()
    if skip_codecs:
        filtered = []
        for p in out:
            info = probe(p)
            if info is None:
                filtered.append(p)   # nicht lesbar -> spaeter als SKIP melden
                continue
            if info["vcodec"].lower() in skip_codecs:
                continue             # schon effizient -> ueberspringen
            filtered.append(p)
        out = filtered
    if limit:
        out = out[:limit]
    return out


# ---------- Hauptlauf ----------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Videos verlustarm komprimieren mit QS + Platz-Ueberwachung")
    ap.add_argument("root", nargs="?", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="wirklich konvertieren (sonst nur Trockenlauf)")
    ap.add_argument("--delete", action="store_true",
                    help="Original nach erfolgreicher QS SOFORT loeschen "
                         "(statt in den Papierkorb zu verschieben)")
    ap.add_argument("--codec", choices=["hevc", "av1"], default="hevc")
    ap.add_argument("--crf", type=int, default=None,
                    help="Qualitaet (hevc default 21, av1 default 30); "
                         "kleiner = besser/groesser")
    ap.add_argument("--preset", default=None,
                    help="ffmpeg-Preset (hevc default 'medium', av1 '6')")
    ap.add_argument("--reserve-gb", type=float, default=10.0,
                    help="Pause, wenn weniger als N GB frei sind (default 10)")
    ap.add_argument("--min-mb", type=float, default=5.0,
                    help="Videos kleiner als N MB ueberspringen (default 5)")
    ap.add_argument("--skip-codecs", default="hevc,h265,av1",
                    help="schon effizient kodierte ueberspringen "
                         "(default hevc,h265,av1; '' = nichts ueberspringen)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--tol-sec", type=float, default=0.5,
                    help="erlaubte Laengen-Abweichung in s (default 0.5)")
    ap.add_argument("--empty-trash", action="store_true",
                    help="Papierkorb endgueltig loeschen und beenden")
    args = ap.parse_args()

    root = resolve_root(args.root)
    trash = root / TRASH_NAME
    crf = args.crf if args.crf is not None else (30 if args.codec == "av1" else 21)
    preset = args.preset or ("6" if args.codec == "av1" else "medium")

    # --- Papierkorb leeren ---
    if args.empty_trash:
        sz = trash_size(trash)
        if not trash.is_dir() or sz == 0:
            print(f"Papierkorb ist leer oder nicht vorhanden: {trash}")
            return
        print(f"Papierkorb: {trash}\nInhalt: {_mb(sz)}")
        shutil.rmtree(trash)
        print(f"Endgueltig geloescht. {_mb(sz)} frei geworden.")
        return

    skip = {c.strip().lower() for c in args.skip_codecs.split(",") if c.strip()}
    skip_codecs = skip or None

    print(f"Wurzelverzeichnis: {root}")
    print(f"Codec: {args.codec} (crf={crf}, preset={preset})")
    print(f"Modus: {'APPLY' if args.apply else 'Trockenlauf (keine Aenderung)'}"
          f"{'  + Originale SOFORT loeschen' if args.apply and args.delete else ''}")
    if not args.delete:
        print(f"Papierkorb: {trash}")
    print(f"Platz-Reserve: {args.reserve_gb:.1f} GB  |  frei: "
          f"{free_bytes(root)/1e9:.1f} GB")
    if skip_codecs:
        print(f"Ueberspringe bereits: {', '.join(sorted(skip_codecs))}")
    print("\nsammle Videos (ffprobe pro Datei, kann dauern)...")

    vids = candidates(root, skip_codecs, args.min_mb, args.limit)
    print(f"Kandidaten: {len(vids)}\n")
    if not vids:
        print("Nichts zu konvertieren.")
        return

    done = failed = skipped = db_hits = 0
    saved = 0
    t_start = time.time()

    for i, src in enumerate(vids, 1):
        rel = str(src.relative_to(root))
        label = f"[{i}/{len(vids)}] {rel}"

        # --- Platz-Wache VOR jeder Konvertierung ---
        free = free_bytes(root)
        if free < args.reserve_gb * 1e9:
            print(f"\n{'='*60}")
            print(f"PAUSE: nur noch {free/1e9:.1f} GB frei "
                  f"(Reserve {args.reserve_gb:.1f} GB).")
            if not args.delete:
                print(f"Papierkorb enthaelt {_mb(trash_size(trash))}:")
                print(f"  {trash}")
                print("\nJetzt: Papierkorb pruefen, dann freigeben mit")
                print("  ./run.sh convert --empty-trash")
                print("und anschliessend diesen Lauf einfach neu starten -")
                print("bereits konvertierte Videos werden uebersprungen.")
            else:
                print("Platte voll - bitte Platz schaffen und neu starten.")
            print("="*60)
            break

        info = probe(src)
        if info is None:
            print(f"{label}: SKIP (nicht lesbar - evtl. './run.sh fix-videos')")
            skipped += 1
            continue

        if not args.apply:
            print(f"{label}: {_mb(info['size'])}, {info['vcodec']}, "
                  f"{info['width']}x{info['height']}, {info['duration']:.1f}s"
                  f"{'' if info['n_audio'] else ', OHNE Audio'}")
            done += 1
            continue

        tmp = src.with_name(src.stem + TMP_MARK + src.suffix)
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass

        t0 = time.time()
        cmd = build_encode_cmd(src, tmp, args.codec, crf, preset)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=None)
        except Exception as e:
            print(f"{label}: ffmpeg-Fehler: {e}")
            failed += 1
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
            continue
        if r.returncode != 0 or not tmp.is_file():
            msg = (r.stderr or "").strip().splitlines()[-1:] or ["unbekannt"]
            print(f"{label}: ENCODE fehlgeschlagen: {msg[0][:120]}")
            failed += 1
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
            continue

        ok, why = qs_check(info, tmp, args.tol_sec)
        if not ok:
            print(f"{label}: QS NICHT bestanden ({why}) - Original bleibt")
            failed += 1
            try: tmp.unlink()
            except OSError: pass
            continue

        new_size = tmp.stat().st_size
        # --- Original wegraeumen, dann konvertierte an den Originalpfad ---
        try:
            if args.delete:
                src.unlink()
            else:
                dest = trash / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dest)      # gleiches Dateisystem -> instant
            os.replace(tmp, src)           # gleiches Verzeichnis -> instant
        except OSError as e:
            print(f"{label}: Ersetzen fehlgeschlagen: {e} - Original bleibt")
            failed += 1
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
            continue

        dbmsg = db_update_converted(root, common.encode_surrogates(rel), src)
        if dbmsg == "aktualisiert":
            db_hits += 1
        pct = (1 - new_size / info["size"]) * 100
        saved += info["size"] - new_size
        done += 1
        print(f"{label}: {_mb(info['size'])} -> {_mb(new_size)} "
              f"(-{pct:.0f}%, {time.time()-t0:.0f}s, DB {dbmsg})")

    # --- Zusammenfassung ---
    print("\n" + "-"*60)
    el = time.time() - t_start
    if args.apply:
        print(f"Fertig: {done} konvertiert, {failed} fehlgeschlagen, "
              f"{skipped} uebersprungen  ({el/60:.1f} min)")
        print(f"Gespart: {saved/1e9:.2f} GB  |  frei jetzt: "
              f"{free_bytes(root)/1e9:.1f} GB")
        if not args.delete and trash.is_dir():
            print(f"Papierkorb: {_mb(trash_size(trash))} in {trash}")
            print("  pruefen, dann:  ./run.sh convert --empty-trash")
        if db_hits:
            print(f"\nDB: {db_hits} Eintraege aktualisiert (Groesse/Zeitstempel), "
                  f"Tags behalten.")
            print("Ein spaeterer Re-Scan sieht die Dateien damit als unveraendert.")
        if done and db_hits < done:
            print(f"\nHinweis: {done - db_hits} konvertierte Videos waren nicht in "
                  f"der DB.\n  -> './run.sh tag' einlesen (werden dann getaggt).")
    else:
        print(f"Trockenlauf: {done} Kandidaten, {skipped} nicht lesbar ({el/60:.1f} min)")
        print("-> Konvertieren:  ./run.sh convert --apply")


if __name__ == "__main__":
    main()
