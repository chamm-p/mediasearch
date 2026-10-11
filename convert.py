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
from common import (connect, db_path, decode_surrogates,
                    extract_video_frame, root_slot, thumb_path,
                    video_duration)

HERE = Path(__file__).resolve().parent
TRASH_NAME = ".mediasearch-converted-originals"   # auch in common.EXCLUDE_DIRS!
TMP_MARK = ".msconv"

# Gruende, warum ein Video nicht konvertiert wurde (fuer den Report).
# 'ok' = konvertiert. Alles andere landet im Log mit Begruendung.
R_OK         = "konvertiert"
R_ALREADY    = "bereits effizient"      # schon hevc/av1 - kein Handlungsbedarf
R_TOO_SMALL  = "zu klein"               # unter --min-mb
R_UNREADABLE = "nicht lesbar"           # ffprobe/Decoder scheitert (flv, rm, ...)
R_ENCODE     = "encode fehlgeschlagen"  # ffmpeg-Fehler
R_QS         = "QS nicht bestanden"     # Laenge/Audio/Aufloesung/groesser
R_REPLACE    = "ersetzen fehlgeschlagen"
R_LIMIT      = "nicht bearbeitet (--limit)"
R_NOGAIN     = "bringt nichts (gemerkt)"   # frueher schon: nicht kleiner
R_DRYRUN     = "wuerde konvertiert"


class Report:
    """Sammelt pro Datei das Ergebnis und schreibt am Ende ein Log, das nach
    Grund gruppiert ist - damit man bei tausenden Videos sieht, was warum
    liegen geblieben ist (z.B. alte flv/rm-Dateien, die kein Decoder mag)."""

    def __init__(self, path: Path | None):
        self.path = path
        self.rows: list[tuple[str, str, str]] = []   # (grund, rel, detail)

    def add(self, reason: str, rel: str, detail: str = "") -> None:
        # Zentral absichern: ein Dateiname mit defekten Bytes darf das
        # Log-Schreiben nicht gefaehrden, auch wenn ein Aufrufer _safe()
        # vergisst.
        self.rows.append((reason, _safe(rel), _safe(detail)))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for reason, _rel, _d in self.rows:
            out[reason] = out.get(reason, 0) + 1
        return out

    @staticmethod
    def rotate(path: Path, max_mb: float, keep: int = 3) -> None:
        """Logrotate: ist das Log groesser als max_mb, wird es zu .1
        weggedreht (.1 -> .2 usw.), aelteste fallen raus. So bleibt EIN
        aktuelles Log, ohne unbegrenzt zu wachsen."""
        try:
            if not path.is_file() or path.stat().st_size < max_mb * 1e6:
                return
            for i in range(keep, 0, -1):
                older = path.with_suffix(path.suffix + f".{i}")
                newer = (path if i == 1
                         else path.with_suffix(path.suffix + f".{i-1}"))
                if newer.exists():
                    if i == keep and older.exists():
                        older.unlink()
                    newer.replace(older)
        except OSError as e:
            print(f"WARNUNG: Logrotate fehlgeschlagen: {e}")

    def write(self, root: Path, header: dict) -> Path | None:
        """Schreibt das Log. Konvertierte kommen mit rein (Nachvollzieh-
        barkeit), die Problemfaelle stehen aber zuerst."""
        if self.path is None:
            return None
        order = [R_UNREADABLE, R_QS, R_ENCODE, R_REPLACE, R_ALREADY,
                 R_NOGAIN, R_TOO_SMALL, R_LIMIT, R_DRYRUN, R_OK]
        by: dict[str, list[tuple[str, str]]] = {}
        for reason, rel, detail in self.rows:
            by.setdefault(reason, []).append((rel, detail))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # ANHAENGEN: ein einziges Log ueber alle Laeufe, mit klarer
            # Trennung je Lauf. Rotation haelt die Datei in Groesse.
            with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                f.write("\n\n" + "#" * 70 + "\n")
                f.write(f"# LAUF {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("#" * 70 + "\n")
                f.write(f"Zeitpunkt : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Root      : {root}\n")
                for k, v in header.items():
                    f.write(f"{k:<10}: {v}\n")
                f.write("\nZusammenfassung:\n")
                for reason in order:
                    n = len(by.get(reason, []))
                    if n:
                        f.write(f"  {reason:<24} {n}\n")
                for reason in order:
                    items = by.get(reason)
                    if not items:
                        continue
                    f.write(f"\n{'='*70}\n{reason.upper()}  ({len(items)})\n"
                            f"{'='*70}\n")
                    if reason == R_UNREADABLE:
                        f.write("Diese Dateien konnte kein Decoder oeffnen "
                                "(alte Formate wie flv/rm, oder defekt).\n"
                                "-> Reparaturversuch: ./run.sh fix-videos\n\n")
                    elif reason == R_NOGAIN:
                        f.write("Beim letzten Versuch wurde das Ergebnis NICHT "
                                "kleiner (schon sparsam kodiert, verrauscht\n"
                                "oder sehr kurz). Gemerkt in convert-skip.json, "
                                "damit sie nicht jedes Mal\nneu durch den "
                                "Encoder laufen. Erneut probieren:\n"
                                "  ./run.sh convert --apply --retry-nogain "
                                "--crf 28\n\n")
                    elif reason == R_ALREADY:
                        f.write("Schon effizient kodiert (hevc/av1) - "
                                "absichtlich uebersprungen, erneutes Encoden\n"
                                "wuerde nur Qualitaet kosten.\n\n")
                    elif reason == R_QS:
                        f.write("Konvertierung lief, hat aber die "
                                "Qualitaetssicherung nicht bestanden.\n"
                                "Das Original ist unangetastet geblieben.\n\n")
                    for rel, detail in sorted(items):
                        f.write(f"  {rel}" + (f"   [{detail}]" if detail else "") + "\n")
            return self.path
        except OSError as e:
            print(f"WARNUNG: Log konnte nicht geschrieben werden: {e}")
            return None


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


# --- Merkliste fuer "bringt nichts"-Videos -----------------------------------
# Videos, bei denen die Konvertierung zwar technisch klappt, das Ergebnis aber
# NICHT kleiner wird (schon sparsam kodiert, stark verrauscht, sehr kurz),
# wuerden bei jedem Lauf erneut durch den Encoder laufen - fuer ein absehbares
# Ergebnis. Deshalb merken wir sie pro Medien-Root in einer JSON-Datei im
# Daten-Slot (neben DB/Backups) und ueberspringen sie kuenftig.
#
# Der Schluessel enthaelt Groesse+mtime: aendert sich die Datei (neu
# aufgenommen, ersetzt), gilt der Eintrag nicht mehr und es wird neu probiert.
SKIP_FILE = "convert-skip.json"


def _skip_path(root: Path) -> Path:
    return root_slot(root) / SKIP_FILE


def load_skiplist(root: Path) -> dict:
    p = _skip_path(root)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_skiplist(root: Path, data: dict) -> None:
    p = _skip_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        # errors="replace" als letzte Absicherung: die Merkliste darf NIE
        # den ganzen Lauf abbrechen, selbst wenn doch ein defektes Zeichen
        # durchkommt.
        payload = json.dumps(data, indent=1, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8", errors="replace") as f:
            f.write(payload)
        tmp.replace(p)
    except (OSError, ValueError, UnicodeError) as e:
        print(f"WARNUNG: Merkliste nicht speicherbar: {e}")


def skip_key(rel: str, size: int, mtime: float) -> str:
    """rel + Groesse + mtime(sekundengenau). Aendert sich die Datei,
    passt der Schluessel nicht mehr -> erneuter Versuch.

    WICHTIG: rel laeuft durch _safe(). Dateinamen mit defekten Bytes
    (lone surrogates) lassen sich NICHT als JSON/UTF-8 schreiben - sonst
    knallt save_skiplist mit UnicodeEncodeError. Da _safe() hier zentral
    angewendet wird, erzeugen Schreib- und Lese-Pfad garantiert denselben
    Schluessel. Die Ersetzung ist deterministisch, Kollisionen sind
    durch Groesse+mtime praktisch ausgeschlossen."""
    return f"{_safe(rel)}|{size}|{int(mtime)}"


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
    # Eingebettete Cover-Bilder/Thumbnails meldet ffprobe ebenfalls als
    # codec_type=video (z.B. 120x90 png mit disposition attached_pic). Wuerde
    # man einfach vs[0] nehmen, bekaeme man die Aufloesung des VORSCHAUBILDS
    # -> QS meldet faelschlich "Aufloesung weicht ab".
    real = [x for x in vs
            if not (x.get("disposition", {}) or {}).get("attached_pic")
            and (x.get("codec_name") or "") not in ("png", "mjpeg", "bmp", "gif")]
    if not real:
        real = vs           # nur Cover vorhanden -> nichts zu filtern
    # groesster echter Stream = das eigentliche Video
    v = max(real, key=lambda x: (int(x.get("width") or 0) * int(x.get("height") or 0)))
    n_cover = len(vs) - len(real)
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
        "n_cover": n_cover,          # eingebettete Thumbnails
        "v_index": int(v.get("index") or 0),
    }


def diagnose_unreadable(path: Path) -> str:
    """Warum ist diese Datei nicht lesbar? Kurzer, aussagekraeftiger Grund
    fuer das Protokoll - damit man nicht extra './run.sh fix-videos --why'
    aufrufen muss. Nutzt die ECHTE ffmpeg/ffprobe-Meldung."""
    # 1) ffprobe: was sieht er ueberhaupt?
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60)
        perr = (pr.stderr or "").strip()
        streams = (pr.stdout or "").strip()
    except Exception as e:
        return f"ffprobe nicht ausfuehrbar: {e}"
    # 2) Frame-Versuch mit sichtbarem Fehler
    try:
        fr = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error",
             "-err_detect", "ignore_err", "-fflags", "+genpts",
             "-i", str(path), "-frames:v", "1",
             "-f", "image2", "-vcodec", "mjpeg", "-y", os.devnull],
            capture_output=True, text=True, timeout=120)
        ferr = (fr.stderr or "").strip()
        frc = fr.returncode
    except Exception as e:
        ferr, frc = str(e), -1

    msg = (ferr.splitlines()[0] if ferr else "") or \
          (perr.splitlines()[0] if perr else "")
    low = (msg + " " + perr).lower()
    # Klassifizieren - das Wichtigste zuerst
    if "no such file" in low or not path.exists():
        return "Datei nicht gefunden"
    if path.stat().st_size == 0:
        return "Datei ist 0 Byte"
    if "invalid data found" in low or "moov atom not found" in low:
        return f"Datei defekt/unvollstaendig ({msg[:90]})"
    if "decoder" in low and ("not found" in low or "no decoder" in low):
        return f"Decoder fehlt ({msg[:90]}) -> libavcodec-extra installieren"
    if "permission denied" in low:
        return "keine Leserechte"
    if not streams:
        return f"ffprobe findet keine Streams ({(perr or msg)[:90]})"
    if "video" not in streams:
        return f"kein Video-Stream, nur: {streams.replace(chr(10), ' ')[:70]}"
    if frc == 0 and not ferr:
        return "Frame geht JETZT -> './run.sh tag <root> --retry-errors'"
    return f"Frame-Extraktion scheitert ({msg[:100] or 'rc=' + str(frc)})"


def decodes_ok(path: Path) -> bool:
    """Prueft, dass echt dekodiert werden kann (nicht nur Header lesbar)."""
    return common.extract_video_frame(path, 0.0) is not None


# Container, die HEVC/AV1 korrekt speichern koennen. Alte Container (avi,
# wmv, flv, mpg...) kennen keine passenden Codec-Tags: ffmpeg schreibt dann
# entweder gar nicht (-> "Error opening output files: Invalid argument") oder
# eine Datei, die als 'rawvideo' gelesen wird und praktisch unbrauchbar ist.
# Deshalb landet das Ergebnis bei solchen Quellen in einem .mp4 (bzw. .mkv,
# wenn mehrere Tonspuren/Untertitel dabei sind, die mp4 nicht mag).
MODERN_CONTAINERS = {".mp4", ".mkv", ".mov", ".m4v", ".webm"}


def target_suffix(src: Path, codec: str, info: dict) -> str:
    """Welche Endung soll das Ergebnis haben?

    Behaelt die Original-Endung, wenn der Container HEVC/AV1 kann.
    Sonst .mkv (vielseitigster Container) bzw. .mp4 fuer den Normalfall."""
    suf = src.suffix.lower()
    if suf in MODERN_CONTAINERS:
        if suf == ".webm" and codec.startswith("hevc") or \
           suf == ".webm" and codec in ("hevc", "nvenc", "qsv", "vaapi"):
            return ".mkv"          # webm darf kein HEVC enthalten
        return src.suffix
    # Alter Container -> umpacken. mkv schluckt alles (auch mehrere
    # Tonspuren, Untertitel, ungewoehnliche Audio-Codecs wie wmav2).
    return ".mkv"


# ---------- Encoder ----------

# Encoder-Profile. Wichtig: Hardware-Encoder (NVENC/VAAPI/QSV) kennen
# WEDER -crf NOCH die libx265-Presets - sie nutzen eigene Qualitaets-Flags
# (-cq bzw. -qp/-global_quality) und eigene Preset-Namen (p1..p7). Deshalb
# pro Encoder ein eigenes Profil statt einer gemeinsamen Codezeile.
#   quality_flag: wie wird die Qualitaet gesetzt
#   default_q   : sinnvoller Startwert (visuell verlustfrei)
#   preset      : Default-Preset
#   hw          : Hardware-Encoder? (nur Info fuer die Ausgabe)
ENCODERS: dict[str, dict] = {
    "hevc": {           # CPU, beste Kompression pro Bit, langsam
        "vcodec": "libx265", "quality_flag": "-crf", "default_q": 21,
        "preset": "medium", "hw": False,
        "extra": ["-pix_fmt", "yuv420p", "-tag:v", "hvc1"],
    },
    "av1": {            # CPU, kleinste Dateien, sehr langsam
        "vcodec": "libsvtav1", "quality_flag": "-crf", "default_q": 26,
        "preset": "6", "hw": False,
        "extra": ["-pix_fmt", "yuv420p10le", "-svtav1-params", "tune=0"],
    },
    "nvenc": {          # NVIDIA-GPU, 10-20x schneller als libx265
        "vcodec": "hevc_nvenc", "quality_flag": "-cq", "default_q": 24,
        "preset": "p5", "hw": True,
        # -rc vbr + -cq = qualitaetsbasiert (wie CRF). p5 = guter
        # Kompromiss, p7 = langsamer/kleiner. -tag:v hvc1 fuer Kompatibilitaet.
        "extra": ["-rc", "vbr", "-b:v", "0", "-pix_fmt", "yuv420p",
                  "-tag:v", "hvc1"],
    },
    "av1nvenc": {       # NVIDIA AV1 - nur RTX 40xx/Ada und neuer!
        "vcodec": "av1_nvenc", "quality_flag": "-cq", "default_q": 24,
        "preset": "p5", "hw": True,
        "extra": ["-rc", "vbr", "-b:v", "0", "-pix_fmt", "yuv420p"],
    },
    "av1qsv": {         # Intel Arc / ab Alder Lake mit Arc-GPU
        "vcodec": "av1_qsv", "quality_flag": "-global_quality",
        "default_q": 24, "preset": "medium", "hw": True,
        "extra": [],
    },
    "av1vaapi": {       # AMD RDNA3 / Intel Arc unter Linux
        "vcodec": "av1_vaapi", "quality_flag": "-qp", "default_q": 24,
        "preset": "", "hw": True,
        "extra": [],
        "pre_input": ["-vaapi_device", "/dev/dri/renderD128"],
        "filter": "format=nv12,hwupload",
    },
    "vaapi": {          # Intel/AMD-GPU unter Linux
        "vcodec": "hevc_vaapi", "quality_flag": "-qp", "default_q": 24,
        "preset": "", "hw": True,
        "extra": ["-tag:v", "hvc1"],
        "pre_input": ["-vaapi_device", "/dev/dri/renderD128"],
        "filter": "format=nv12,hwupload",
    },
    "qsv": {            # Intel QuickSync
        "vcodec": "hevc_qsv", "quality_flag": "-global_quality",
        "default_q": 24, "preset": "medium", "hw": True,
        "extra": ["-tag:v", "hvc1"],
    },
}


def available_encoders() -> set[str]:
    """Welche unserer Encoder kennt das installierte ffmpeg wirklich?"""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=30)
        out = r.stdout or ""
    except Exception:
        return set()
    return {name for name, prof in ENCODERS.items() if prof["vcodec"] in out}


def build_encode_cmd(src: Path, dst: Path, codec: str, quality: int,
                     preset: str, v_index: int = 0) -> list[str]:
    """ffmpeg-Kommando. Audio/Untertitel werden unveraendert kopiert
    (-c:a copy), nur das Video wird neu kodiert.

    WICHTIG: gemappt wird genau EIN Video-Stream (v_index) + alle Audio-
    und Untertitel-Streams - NICHT '-map 0'. Sonst wuerde ein eingebettetes
    Cover-Bild als zweiter Video-Stream mitkodiert; das laesst den Encode
    fehlschlagen bzw. blaeht das Ergebnis auf."""
    prof = ENCODERS[codec]
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"]
    cmd += prof.get("pre_input", [])
    cmd += ["-i", str(src),
            "-map", f"0:{v_index}", "-map", "0:a?", "-map", "0:s?",
            "-map_metadata", "0"]
    if prof.get("filter"):
        cmd += ["-vf", prof["filter"]]
    cmd += ["-c:v", prof["vcodec"], prof["quality_flag"], str(quality)]
    if preset:
        cmd += ["-preset", preset]
    cmd += prof.get("extra", [])
    # Audio/Untertitel unveraendert; Daten-Streams verwerfen (machen oft Aerger)
    cmd += ["-c:a", "copy", "-c:s", "copy", "-dn"]
    # +faststart ist eine MP4/MOV-Eigenschaft (moov nach vorn) - bei anderen
    # Containern sinnlos.
    if dst.suffix.lower() in (".mp4", ".mov", ".m4v"):
        cmd += ["-movflags", "+faststart"]
    cmd += [str(dst)]
    return cmd


# ---------- QS ----------

def qs_check(orig: dict, new_path: Path, tol_sec: float,
             want_codec: str | None = None) -> tuple[bool, str]:
    """Technische Qualitaetssicherung VOR dem Loeschen des Originals.
    Jede Pruefung ist ein Abbruchkriterium."""
    new = probe(new_path)
    if new is None:
        return False, "nicht lesbar / kein Video-Stream"
    if not decodes_ok(new_path):
        return False, "kein Frame dekodierbar"
    # Container-Falle: z.B. HEVC in .avi erzeugt eine Datei, die als
    # 'rawvideo' gelesen wird - technisch da, praktisch unbrauchbar.
    if want_codec and new["vcodec"].lower() not in (want_codec, "hevc", "av1"):
        return False, (f"falscher Codec im Ergebnis: {new['vcodec']} "
                       f"statt {want_codec} (Container passt nicht)")
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


def _is_clean(s: str) -> bool:
    """True, wenn der String als UTF-8 schreibbar ist (keine defekten Bytes)."""
    try:
        s.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _safe(s: str) -> str:
    """Macht beliebige Dateinamen ausgabe- und logbar.

    Dateinamen mit defekter Kodierung liefert os.walk als str mit lone
    surrogates. Die lassen sich WEDER ins Terminal noch in eine UTF-8-Datei
    schreiben -> UnicodeEncodeError mitten im Lauf. serve.py hat dafuer
    dieselbe Absicherung."""
    if not s:
        return s or ""
    if _is_clean(s):
        return s
    return s.encode("utf-8", errors="replace").decode("utf-8")


def fix_name(rel: str) -> str:
    """Repariert einen kaputt kodierten Dateinamen (Pfad RELATIV zum Root).

    Defekte Namen kommen als lone surrogates an. Erst werden die Original-
    Bytes zurueckgeholt (surrogateescape) und als cp1252/latin-1 gelesen -
    der haeufigste Fall (Umlaute von alten Windows-Systemen auf einem
    UTF-8-Dateisystem); daraus wird dann korrekt z.B. 'Muenchen' mit echtem
    Umlaut. Klappt das nicht, werden die defekten Zeichen durch '_' ersetzt.
    Jeder Pfad-Teil einzeln, damit Verzeichnisnamen unangetastet bleiben,
    wenn nur der Dateiname betroffen ist."""
    if _is_clean(rel):
        return rel
    parts = []
    for seg in rel.split(os.sep):
        if _is_clean(seg):
            parts.append(seg)
            continue
        fixed = None
        try:
            raw = seg.encode("utf-8", "surrogateescape")
        except Exception:
            raw = None
        if raw is not None:
            for enc in ("utf-8", "cp1252", "latin-1"):
                try:
                    cand = raw.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
                if _is_clean(cand) and cand.strip():
                    fixed = cand
                    break
        if fixed is None:
            fixed = seg.encode("utf-8", "replace").decode("utf-8", "replace")
            fixed = fixed.replace(chr(0xFFFD), "_")
        parts.append(fixed)
    return os.sep.join(parts)


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

def db_update_converted(root: Path, rel: str, new_abs: Path,
                        new_rel: str | None = None) -> str:
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
        if new_rel and new_rel != rel:
            # Name wurde repariert -> rel_path + sort_key mitnehmen, sonst
            # verliert die Datei beim naechsten Scan ihre Tags (sie waere
            # "verschwunden" und der neue Name "neu").
            conn.execute(
                "UPDATE files SET rel_path=?, sort_key=?, size=?, mtime=?, "
                "content_hash='', phash_int=NULL WHERE id=?",
                (new_rel, common.natural_key(new_rel), st.st_size,
                 st.st_mtime, row["id"]))
            conn.commit()
            return "aktualisiert + umbenannt"
        conn.execute(
            "UPDATE files SET size=?, mtime=?, content_hash='', phash_int=NULL "
            "WHERE id=?", (st.st_size, st.st_mtime, row["id"]))
        conn.commit()
        return "aktualisiert"
    finally:
        conn.close()


# ---------- Kandidaten ----------

def candidates(root: Path, skip_codecs: set[str] | None, min_mb: float,
               limit: int | None, report: "Report",
               skiplist: dict | None = None) -> list[Path]:
    """Alle Videos unter root (rekursiv), ohne Papierkorb und Temp-Reste.
    skip_codecs: bereits effizient kodierte Videos AUSSCHLIESSEN (z.B. hevc/av1
    nicht nochmal durch den Encoder schicken - das kostet nur Qualitaet).

    Uebersprungene werden im Report mit Grund vermerkt (nicht still verworfen),
    damit man hinterher sieht, warum eine Datei nicht angefasst wurde."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d != TRASH_NAME and d not in common.EXCLUDE_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if common.media_type(p) != "video":
                continue
            if TMP_MARK in p.name:
                continue
            rel = _safe(str(p.relative_to(root)))
            try:
                p.stat()
            except OSError as e:
                report.add(R_UNREADABLE, rel, f"stat: {e}")
                continue
            found.append(p)
    found.sort()

    # Reihenfolge der Pruefungen ist wichtig fuer die Aussagekraft des Logs:
    # zuerst LESBARKEIT (sonst wuerde eine unlesbare kleine Datei als
    # "zu klein" protokolliert und der echte Grund waere verschleiert),
    # dann Codec, dann Groesse.
    out: list[Path] = []
    n = len(found)
    checked = 0
    for i, p in enumerate(found, 1):
        # FRUEH-ABBRUCH: sobald das Limit erreicht ist, nicht weiter proben.
        # Sonst wuerde '--limit 100' trotzdem ffprobe ueber ALLE (z.B. 5000)
        # Dateien laufen lassen - bei grossen Sammlungen minutenlang umsonst.
        if limit and len(out) >= limit:
            for rest in found[i-1:]:
                report.add(R_LIMIT, _safe(str(rest.relative_to(root))),
                           "nicht geprueft (Limit erreicht)")
            print(f"  Limit {limit} erreicht - restliche "
                  f"{len(found) - (i-1)} Dateien nicht mehr geprueft.", flush=True)
            break
        rel = _safe(str(p.relative_to(root)))
        checked = i
        if i % 200 == 0 or i == n:
            print(f"  ...geprueft {i}/{n}", flush=True)
        info = probe(p)
        if info is None:
            # Kein Decoder kommt ran (alte flv/rm, defekt) - nicht erst
            # encoden lassen, direkt mit Grund protokollieren.
            report.add(R_UNREADABLE, rel, diagnose_unreadable(p))
            continue
        if skip_codecs and info["vcodec"].lower() in skip_codecs:
            report.add(R_ALREADY, rel, f"{info['vcodec']}, {_mb(info['size'])}")
            continue
        if info["size"] < min_mb * 1e6:
            report.add(R_TOO_SMALL, rel,
                       f"{_mb(info['size'])}, {info['vcodec']}")
            continue
        # Frueher schon erfolglos (Ergebnis war nicht kleiner)? Dann nicht
        # wieder encoden - der Schluessel enthaelt Groesse+mtime, bei einer
        # geaenderten Datei wird also erneut probiert.
        if skiplist is not None:
            try:
                st = p.stat()
            except OSError:
                st = None
            if st is not None:
                k = skip_key(str(p.relative_to(root)), st.st_size, st.st_mtime)
                if k in skiplist:
                    report.add(R_NOGAIN, rel, skiplist[k].get("why", "")[:80]
                               if isinstance(skiplist[k], dict) else "")
                    continue
        out.append(p)
    return out


# ---------- Hauptlauf ----------

def cmd_restore_trash(root: Path, trash: Path, dry: bool,
                      only: str | None) -> None:
    """Stellt Originale aus dem Papierkorb wieder her.

    Pro Original wird:
      1) die konvertierte Datei am Zielpfad entfernt (sonst laegen beide da;
         beachtet auch einen Container-Wechsel, z.B. film.avi -> film.mkv)
      2) das Original zurueckverschoben
      3) der DB-Eintrag auf das Original zurueckgesetzt (Pfad/Groesse/mtime),
         Tags bleiben erhalten
    """
    if not trash.is_dir():
        print(f"Kein Papierkorb vorhanden: {trash}")
        return
    originals = [p for p in trash.rglob("*") if p.is_file()]
    if only:
        originals = [p for p in originals if only.lower() in str(p).lower()]
    if not originals:
        print("Papierkorb ist leer" + (f" (Filter '{only}')" if only else ""))
        return

    print(f"Papierkorb: {trash}")
    print(f"{len(originals)} Original(e)"
          + (f" (Filter '{only}')" if only else "")
          + ("  [Trockenlauf]" if dry else "  [WIEDERHERSTELLEN]"))
    print()
    done = skipped = 0
    for src_bak in sorted(originals):
        rel = str(src_bak.relative_to(trash))
        rel_out = _safe(rel)
        target = root / rel                      # Originalpfad
        # Die konvertierte Datei kann eine andere Endung haben (.avi -> .mkv)
        converted = [target] + [target.with_suffix(s)
                                for s in sorted(MODERN_CONTAINERS)
                                if s != target.suffix.lower()]
        present = [c for c in converted if c.is_file() and c != src_bak]
        if dry:
            info = ", ".join(_safe(c.name) for c in present) or "keine"
            print(f"  {rel_out}\n      konvertierte Datei: {info}")
            done += 1
            continue
        try:
            # 1) konvertierte Datei(en) entfernen
            for c in present:
                c.unlink()
            # 2) Original zurueck
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src_bak, target)
        except OSError as e:
            print(f"  {rel_out}: FEHLER {e}")
            skipped += 1
            continue
        # 3) DB zurueckstellen: Eintrag kann auf die konvertierte Datei
        #    (evtl. andere Endung) zeigen -> auf das Original umbiegen.
        st = target.stat()
        stored_new = common.encode_surrogates(rel)
        conn = connect(root)
        try:
            row = None
            for cand in [rel] + [str(Path(rel).with_suffix(c.suffix))
                                 for c in present]:
                row = conn.execute("SELECT id FROM files WHERE rel_path=?",
                                   (common.encode_surrogates(cand),)).fetchone()
                if row:
                    break
            if row:
                conn.execute(
                    "UPDATE files SET rel_path=?, sort_key=?, size=?, mtime=?, "
                    "content_hash='', phash_int=NULL WHERE id=?",
                    (stored_new, common.natural_key(stored_new),
                     st.st_size, st.st_mtime, row["id"]))
                conn.commit()
        finally:
            conn.close()
        print(f"  {rel_out}: wiederhergestellt"
              + (f" (entfernt: {', '.join(_safe(c.name) for c in present)})"
                 if present else ""))
        done += 1

    print("\n" + "-"*60)
    if dry:
        print(f"Trockenlauf: {done} Original(e) wuerden wiederhergestellt.")
        print("-> Wirklich:  ./run.sh convert --restore-trash --apply")
    else:
        print(f"Fertig: {done} wiederhergestellt, {skipped} Fehler.")
        print("Leere Ordner im Papierkorb koennen bleiben.")
        print("Tipp: Merkliste ggf. leeren, damit erneut probiert wird:")
        print("  ./run.sh convert --forget-nogain")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="./run.sh convert",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Videos verlustarm komprimieren (H.265/AV1) mit technischer QS\n"
            "und Platz-Ueberwachung. Rekursiv ab dem Wurzelverzeichnis."),
        epilog="""
Ablauf je Video: encode -> QS -> Original wegraeumen -> DB aktualisieren.
Die QS prueft VOR dem Loeschen: abspielbar, Frame dekodierbar, Laenge wie
Original, Audio erhalten, Aufloesung gleich, Ergebnis kleiner. Scheitert
eine Pruefung, bleibt das Original unangetastet.

Beispiele:
  ./run.sh convert                     Trockenlauf: was waere zu tun? (aendert nichts)
  ./run.sh convert --apply             konvertieren, Originale in den Papierkorb
  ./run.sh convert --apply --limit 5   erst 5 Videos probieren (empfohlen zum Start)
  ./run.sh convert --apply --delete    Originale sofort loeschen (kein Papierkorb)
  ./run.sh convert --apply --codec nvenc --limit 100
                                       NVIDIA-GPU, 10-20x schneller als hevc
  ./run.sh convert --list-encoders     welche Encoder kann dieses ffmpeg?
  ./run.sh convert --apply --codec av1 AV1 statt H.265 (kleiner, aber viel langsamer)
  ./run.sh convert --empty-trash       Papierkorb endgueltig loeschen (Platz frei)
  ./run.sh convert --apply --reserve-gb 25
                                       frueher pausieren (mehr Platz-Puffer)

Platzmangel: faellt der freie Platz unter --reserve-gb, haelt der Lauf an.
Dann Papierkorb pruefen, '--empty-trash', und den Lauf neu starten -
bereits konvertierte Videos werden uebersprungen.

Container: alte Container (avi/divx, wmv, flv, mpg) koennen HEVC/AV1 nicht
korrekt speichern - ffmpeg bricht dann ab ("Error opening output files:
Invalid argument") oder schreibt eine praktisch unbrauchbare Datei. Solche
Videos werden daher nach .mkv umgepackt (Audio/Untertitel unveraendert),
der DB-Pfad wandert mit. .mp4/.mkv/.mov behalten ihre Endung.

Wiederholte Laeufe: bereits konvertierte Videos sind danach HEVC/AV1 und
werden per --skip-codecs uebersprungen - du kannst also jederzeit abbrechen
und denselben Befehl neu starten. Videos, bei denen das Ergebnis NICHT
kleiner wurde, landen in einer Merkliste (convert-skip.json im Daten-Slot)
und werden kuenftig gar nicht erst encodet. Aendert sich die Datei, wird
automatisch neu probiert. --retry-nogain probiert sie trotzdem (z.B. mit
hoeherem --crf), --forget-nogain leert die Liste.

Kaputt kodierte Dateinamen (defekte Bytes von alten Windows-Systemen)
werden beim Konvertieren gleich mitrepariert - der DB-Pfad wandert mit,
Tags bleiben erhalten. Abschalten mit --no-fix-names. Existiert der
reparierte Name schon, bleibt der alte (kein Ueberschreiben).

Mit --limit N wird nach N Kandidaten abgebrochen - auch die Pruef-Phase
stoppt dann (keine unnoetigen ffprobe-Laeufe ueber die restliche Sammlung).

Protokoll: EIN Log (convert.log im Tool-Ordner), jeder Lauf haengt einen
Abschnitt an. Ab --log-max-mb (default 5 MB) wird zu convert.log.1 rotiert
(3 Archive). Enthaelt alle Dateien
nach Grund gruppiert (nicht lesbar / bereits effizient / zu klein / QS
nicht bestanden / konvertiert).
""")
    ap.add_argument("root", nargs="?", default=None,
                    help="Wurzelverzeichnis (default: aus settings.json)")
    ap.add_argument("--apply", action="store_true",
                    help="wirklich konvertieren (sonst nur Trockenlauf)")
    ap.add_argument("--delete", action="store_true",
                    help="Original nach erfolgreicher QS SOFORT loeschen "
                         "(statt in den Papierkorb zu verschieben)")
    ap.add_argument("--codec", choices=sorted(ENCODERS.keys()), default="hevc",
                    help="Encoder. hevc=libx265 (CPU, beste Kompression), "
                         "av1=libsvtav1 (kleiner, sehr langsam), "
                         "nvenc=NVIDIA-GPU (10-20x schneller), "
                         "vaapi/qsv=Intel/AMD-GPU. default hevc")
    ap.add_argument("--list-encoders", action="store_true",
                    help="zeigt, welche Encoder dieses ffmpeg unterstuetzt")
    ap.add_argument("--crf", type=int, default=None,
                    help="Qualitaet; kleiner = besser/groesser. Defaults: "
                         "hevc 21, av1(CPU) 26, GPU-Encoder 24. "
                         "Hardware-Encoder und AV1 brauchen bei gleicher "
                         "Zahl mehr Bitrate-Reserve, daher niedrigere Werte.")
    ap.add_argument("--preset", default=None,
                    help="Encoder-Preset (hevc 'medium', av1 '6', "
                         "nvenc 'p5' (p1 schnell .. p7 klein))")
    ap.add_argument("--reserve-gb", type=float, default=10.0,
                    help="Pause, wenn weniger als N GB frei sind (default 10)")
    ap.add_argument("--min-mb", type=float, default=5.0,
                    help="Videos kleiner als N MB ueberspringen (default 5)")
    ap.add_argument("--skip-codecs", default="hevc,h265,av1",
                    help="schon effizient kodierte ueberspringen "
                         "(default hevc,h265,av1; '' = nichts ueberspringen)")
    ap.add_argument("--limit", type=int, default=None,
                    help="nur die ersten N Videos bearbeiten "
                         "(zum Antesten empfohlen)")
    ap.add_argument("--tol-sec", type=float, default=0.5,
                    help="erlaubte Laengen-Abweichung in s (default 0.5)")
    ap.add_argument("--log", default=None,
                    help="Pfad fuer das Lauf-Protokoll "
                         "(default: convert.log im Tool-Ordner; "
                         "'' = kein Log)")
    ap.add_argument("--retry-nogain", action="store_true",
                    help="Videos erneut probieren, die laut Merkliste nichts "
                         "bringen (z.B. zusammen mit hoeherem --crf)")
    ap.add_argument("--forget-nogain", action="store_true",
                    help="Merkliste leeren und beenden")
    ap.add_argument("--no-fix-names", action="store_true",
                    help="kaputt kodierte Dateinamen NICHT mitreparieren "
                         "(Default: beim Konvertieren gleich korrigieren)")
    ap.add_argument("--log-max-mb", type=float, default=5.0,
                    help="Logrotate: ab dieser Groesse wird convert.log zu "
                         "convert.log.1 weggedreht (default 5 MB, 3 Archive)")
    ap.add_argument("--restore-trash", action="store_true",
                    help="Originale aus dem Papierkorb wiederherstellen "
                         "(Trockenlauf; mit --apply wirklich ausfuehren)")
    ap.add_argument("--only", default=None,
                    help="bei --restore-trash: nur Pfade, die diesen Text "
                         "enthalten")
    ap.add_argument("--empty-trash", action="store_true",
                    help="Papierkorb endgueltig loeschen und beenden")
    args = ap.parse_args()

    # --- Encoder-Uebersicht ---
    if args.list_encoders:
        have = available_encoders()
        print("Encoder dieses ffmpeg-Builds:\n")
        print(f"  {'--codec':<18} {'ffmpeg-Encoder':<15} {'Ergebnis':<9} "
              f"{'Art':<13} Status")
        print("  " + "-"*66)
        for name in sorted(ENCODERS):
            prof = ENCODERS[name]
            mark = "vorhanden" if name in have else "NICHT vorhanden"
            kind = "GPU/Hardware" if prof["hw"] else "CPU"
            target = "AV1" if "av1" in prof["vcodec"] else "H.265/HEVC"
            print(f"  --codec {name:<10} {prof['vcodec']:<15} {target:<9} "
                  f"{kind:<13} {mark}")
        av1_hw = [n for n in ("av1nvenc", "av1qsv", "av1vaapi") if n in have]
        if av1_hw:
            print(f"\nAV1 auf der GPU moeglich: --codec {av1_hw[0]}")
            print(f"  ./run.sh convert --apply --codec {av1_hw[0]} --limit 100")
        elif "av1" in have:
            print("\nAV1 nur ueber CPU (libsvtav1) - korrekt, aber langsam:")
            print("  ./run.sh convert --apply --codec av1 --limit 20")
            print("  (erst mit kleinem Limit die Dauer pro Video messen!)")
        if "nvenc" in have:
            print("\nH.265 auf der GPU (schnellste Variante):")
            print("  ./run.sh convert --apply --codec nvenc --limit 100")
        return

    # Encoder-Pruefung VOR der Root-Auflösung: haengt nicht vom Verzeichnis ab,
    # und ein fehlender Encoder wuerde sonst bei jedem einzelnen Video scheitern.
    prof = ENCODERS[args.codec]
    have = available_encoders()
    if args.codec not in have:
        print(f"FEHLER: Encoder '{prof['vcodec']}' ist in diesem ffmpeg nicht "
              f"vorhanden.")
        print(f"Verfuegbar: {', '.join(sorted(have)) or '(keiner gefunden)'}")
        print("Uebersicht:  ./run.sh convert --list-encoders")
        sys.exit(1)
    crf = args.crf if args.crf is not None else prof["default_q"]
    preset = args.preset if args.preset is not None else prof["preset"]

    root = resolve_root(args.root)
    trash = root / TRASH_NAME

    # --- Papierkorb wiederherstellen ---
    if args.restore_trash:
        cmd_restore_trash(root, trash, dry=not args.apply, only=args.only)
        return

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

    # Merkliste der "bringt nichts"-Videos (pro Medien-Root im Daten-Slot)
    if args.forget_nogain:
        n = len(load_skiplist(root))
        save_skiplist(root, {})
        print(f"Merkliste geleert ({n} Eintraege). Beim naechsten Lauf werden "
              f"diese Videos wieder probiert.")
        return
    skiplist = load_skiplist(root)
    if args.retry_nogain:
        if skiplist:
            print(f"--retry-nogain: {len(skiplist)} gemerkte Videos werden "
                  f"diesmal wieder probiert.")
        skiplist_active = None
    else:
        skiplist_active = skiplist

    # Lauf-Protokoll: Default in den Tool-Ordner (nicht in die Mediathek).
    if args.log is None:
        log_path = HERE / "convert.log"
    elif args.log == "":
        log_path = None
    else:
        log_path = Path(args.log).expanduser()
    report = Report(log_path)
    if log_path is not None:
        # vor dem Lauf rotieren, damit das Log nicht unbegrenzt waechst
        Report.rotate(log_path, args.log_max_mb)

    print(f"Wurzelverzeichnis: {root}")
    print(f"Codec: {args.codec} / {prof['vcodec']}"
          f"{' (GPU)' if prof['hw'] else ' (CPU)'}"
          f"  {prof['quality_flag']}={crf}"
          f"{' preset=' + preset if preset else ''}")
    print(f"Modus: {'APPLY' if args.apply else 'Trockenlauf (keine Aenderung)'}"
          f"{'  + Originale SOFORT loeschen' if args.apply and args.delete else ''}")
    if not args.delete:
        print(f"Papierkorb: {trash}")
    print(f"Platz-Reserve: {args.reserve_gb:.1f} GB  |  frei: "
          f"{free_bytes(root)/1e9:.1f} GB")
    if skip_codecs:
        print(f"Ueberspringe bereits: {', '.join(sorted(skip_codecs))}")
    print("\nsammle Videos (ffprobe pro Datei, kann dauern)...")

    vids = candidates(root, skip_codecs, args.min_mb, args.limit, report,
                      skiplist=skiplist_active)
    pre = report.counts()
    print(f"\nKandidaten: {len(vids)}")
    if pre:
        print("Uebersprungen beim Sammeln:")
        for reason, n in sorted(pre.items()):
            print(f"  {reason:<24} {n}")
    print()
    if not vids:
        print("Nichts zu konvertieren.")
        # Log trotzdem schreiben - genau hier will man die Gruende sehen.
        written = report.write(root, {
            "Codec": f"{args.codec} ({prof['vcodec']}) "
                     f"{prof['quality_flag']}={crf}",
            "Modus": "apply" if args.apply else "Trockenlauf",
            "min-mb": args.min_mb,
            "skip": ", ".join(sorted(skip_codecs)) if skip_codecs else "-",
        })
        if written:
            print(f"Gruende pro Datei: {written}")
        return

    done = failed = skipped = db_hits = fixed_names = nogain_new = 0
    saved = 0
    t_start = time.time()

    for i, src in enumerate(vids, 1):
        rel = str(src.relative_to(root))
        rel_out = _safe(rel)          # nur Ausgabe/Log; rel bleibt der echte Pfad
        label = f"[{i}/{len(vids)}] {rel_out}"

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
            report.add(R_UNREADABLE, rel_out, diagnose_unreadable(src))
            skipped += 1
            continue

        if not args.apply:
            detail = (f"{_mb(info['size'])}, {info['vcodec']}, "
                      f"{info['width']}x{info['height']}, {info['duration']:.1f}s"
                      f"{'' if info['n_audio'] else ', OHNE Audio'}")
            print(f"{label}: {detail}")
            report.add(R_DRYRUN, rel_out, detail)
            done += 1
            continue

        # Zielcontainer: alte Container (avi/wmv/flv...) koennen HEVC/AV1
        # nicht korrekt kennzeichnen -> Ergebnis als .mkv ablegen.
        new_suffix = target_suffix(src, args.codec, info)
        tmp = src.with_name(src.stem + TMP_MARK + new_suffix)
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass

        t0 = time.time()
        cmd = build_encode_cmd(src, tmp, args.codec, crf, preset,
                               v_index=info.get("v_index", 0))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=None)
        except Exception as e:
            print(f"{label}: ffmpeg-Fehler: {e}")
            report.add(R_ENCODE, rel_out, str(e)[:160])
            failed += 1
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
            continue
        if r.returncode != 0 or not tmp.is_file():
            msg = (r.stderr or "").strip().splitlines()[-1:] or ["unbekannt"]
            print(f"{label}: ENCODE fehlgeschlagen: {msg[0][:120]}")
            report.add(R_ENCODE, rel_out, msg[0][:160])
            failed += 1
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
            continue

        want = "av1" if "av1" in ENCODERS[args.codec]["vcodec"] else "hevc"
        ok, why = qs_check(info, tmp, args.tol_sec, want_codec=want)
        if not ok:
            print(f"{label}: QS NICHT bestanden ({why}) - Original bleibt")
            report.add(R_QS, rel_out, why)
            failed += 1
            try: tmp.unlink()
            except OSError: pass
            # "nicht kleiner" ist ein STABILES Ergebnis (Datei unveraendert,
            # gleiche Einstellungen -> gleiches Resultat). Merken, damit der
            # naechste Lauf nicht wieder umsonst encodet. Andere QS-Fehler
            # (Laenge/Audio/Aufloesung) NICHT merken - die koennen auf einen
            # behebbaren Fehler hindeuten.
            if "nicht kleiner" in why:
                st = src.stat()
                skiplist[skip_key(rel, st.st_size, st.st_mtime)] = {
                    "why": why, "codec": info["vcodec"],
                    "when": time.strftime("%Y-%m-%d"),
                }
                nogain_new += 1
            continue

        new_size = tmp.stat().st_size
        # --- Zielpfad bestimmen: kaputt kodierte Namen gleich mitreparieren ---
        # Wir fassen die Datei hier sowieso an, also ist das der richtige
        # Moment. Kollidiert der reparierte Name mit einer existierenden
        # Datei, bleibt der alte Name (kein Ueberschreiben!).
        final_rel, final_abs = rel, src
        renamed = ""
        if not args.no_fix_names and not _is_clean(rel):
            cand_rel = fix_name(rel)
            cand_abs = root / cand_rel
            if cand_rel != rel and not cand_abs.exists():
                final_rel, final_abs = cand_rel, cand_abs
                renamed = f" [Name repariert: {_safe(cand_rel)}]"
            elif cand_abs.exists():
                renamed = " [Name-Fix uebersprungen: Ziel existiert]"
        # Container gewechselt? Dann muss die Endung mitwandern.
        if new_suffix.lower() != src.suffix.lower():
            cand_abs = (root / final_rel).with_suffix(new_suffix)
            if cand_abs.exists() and cand_abs != src:
                renamed += " [Container-Wechsel uebersprungen: Ziel existiert]"
            else:
                final_rel = str(Path(final_rel).with_suffix(new_suffix))
                final_abs = root / final_rel
                renamed += f" [{src.suffix} -> {new_suffix}]"

        # --- Original wegraeumen, dann konvertierte an den Zielpfad ---
        try:
            if args.delete:
                src.unlink()
            else:
                dest = trash / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dest)      # gleiches Dateisystem -> instant
            if final_abs is not src:
                final_abs.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, final_abs)     # gleiches Verzeichnis -> instant
        except OSError as e:
            print(f"{label}: Ersetzen fehlgeschlagen: {e} - Original bleibt")
            report.add(R_REPLACE, rel_out, str(e)[:160])
            failed += 1
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
            continue

        dbmsg = db_update_converted(
            root, common.encode_surrogates(rel), final_abs,
            new_rel=common.encode_surrogates(final_rel)
                    if final_rel != rel else None)
        if dbmsg.startswith("aktualisiert"):
            db_hits += 1
        if renamed:
            fixed_names += 1 if "repariert" in renamed else 0
        pct = (1 - new_size / info["size"]) * 100
        saved += info["size"] - new_size
        done += 1
        report.add(R_OK, rel_out,
                   f"{_mb(info['size'])} -> {_mb(new_size)} (-{pct:.0f}%)")
        print(f"{label}: {_mb(info['size'])} -> {_mb(new_size)} "
              f"(-{pct:.0f}%, {time.time()-t0:.0f}s, DB {dbmsg}){renamed}")

    # --- Merkliste sichern ---
    if nogain_new:
        save_skiplist(root, skiplist)

    # --- Zusammenfassung ---
    print("\n" + "-"*60)
    el = time.time() - t_start
    if args.apply:
        print(f"Fertig: {done} konvertiert, {failed} fehlgeschlagen, "
              f"{skipped} uebersprungen  ({el/60:.1f} min)")
        if nogain_new:
            print(f"Gemerkt als 'bringt nichts': {nogain_new} "
                  f"(werden kuenftig uebersprungen)")
        if fixed_names:
            print(f"Dateinamen repariert: {fixed_names} "
                  f"(defekte Kodierung korrigiert, DB-Pfad mitgezogen)")
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

    # --- Lauf-Protokoll schreiben ---
    counts = report.counts()
    problems = sum(n for r, n in counts.items()
                   if r in (R_UNREADABLE, R_ENCODE, R_QS, R_REPLACE))
    written = report.write(root, {
        "Codec": f"{args.codec} ({prof['vcodec']}) "
                 f"{prof['quality_flag']}={crf} preset={preset or '-'}",
        "Modus": ("apply" + (" --delete" if args.delete else "")
                  if args.apply else "Trockenlauf"),
        "min-mb": args.min_mb,
        "skip": ", ".join(sorted(skip_codecs)) if skip_codecs else "-",
    })
    if counts:
        print("\nProtokoll:")
        for reason, n in sorted(counts.items()):
            print(f"  {reason:<26} {n}")
    if written:
        print(f"\nDetails pro Datei: {written}")
        if problems:
            print(f"  darin {problems} Problemfaelle (nicht lesbar / QS / encode)")


if __name__ == "__main__":
    main()
