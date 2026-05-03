"""Shared helpers: config, DB schema, paths, thumbs.

Persistenz-Layout (alles relativ zum Script-Ordner -> komplett portabel):
  <scripts>/config.toml              # statische Defaults
  <scripts>/settings.json            # UI-State
  <scripts>/mediasearch.log          # logs
  <scripts>/data/<root-hash>/mediasearch.db
  <scripts>/data/<root-hash>/thumbs/<file-id>.jpg
  <scripts>/data/<root-hash>/root.txt   # absoluter Pfad (debug/uebersicht)
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


# --- Surrogate-/invalid-utf8-sicheres rel_path Encoding ---
# Filenames mit invalid UTF-8 Bytes liefert os.walk als Python-strings mit
# lone surrogates (\udcXX). SQLite + JSON koennen die nicht serialisieren.
# Wir kodieren sie reversibel als '__xXX__' fuer Storage; beim File-Oeffnen
# wird zurueckkonvertiert, damit os.open den Original-Pfad findet.
_SURROGATE_RE = re.compile(r"[\udc80-\udcff]")
_MARKER_RE    = re.compile(r"__x([0-9a-f]{2})__")


def encode_surrogates(s: str) -> str:
    if not s:
        return s
    return _SURROGATE_RE.sub(lambda m: f"__x{ord(m.group(0)) - 0xdc00:02x}__", s)


def decode_surrogates(s: str) -> str:
    if not s:
        return s
    return _MARKER_RE.sub(lambda m: chr(0xdc00 + int(m.group(1), 16)), s)


SCRIPTS_DIR    = Path(__file__).resolve().parent
CONFIG_PATH    = SCRIPTS_DIR / "config.toml"
SYNONYMS_PATH  = SCRIPTS_DIR / "synonyms.json"
MANUAL_PATH    = SCRIPTS_DIR / "manual_tags.json"
DATA_DIR       = SCRIPTS_DIR / "data"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def load_disabled_endpoints() -> set[str]:
    """Liest aus settings.json die Liste deaktivierter Endpoint-Labels."""
    sp = SCRIPTS_DIR / "settings.json"
    if not sp.exists():
        return set()
    try:
        import json as _json
        s = _json.loads(sp.read_text(encoding="utf-8"))
        return set(s.get("disabled_endpoints", []))
    except Exception:
        return set()


def load_synonyms() -> dict[str, str]:
    """Liest synonyms.json und gibt {alias: kanon} fuer schnelle Lookups zurueck."""
    if not SYNONYMS_PATH.exists():
        return {}
    import json as _json
    try:
        raw = _json.loads(SYNONYMS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    alias_to_canon: dict[str, str] = {}
    for canon, aliases in raw.items():
        if canon.startswith("_"):
            continue
        canon = canon.strip().lower()
        if not canon:
            continue
        # Kanon zeigt auf sich selbst (idempotent)
        alias_to_canon[canon] = canon
        for a in aliases or []:
            a = str(a).strip().lower()
            if a:
                alias_to_canon[a] = canon
    return alias_to_canon


def load_manual_tags() -> dict[str, list[str]]:
    """Liest manual_tags.json und gibt {gruppe: [tag,...]} zurueck.
    Akzeptiert auch ein flaches Array oder ein Objekt mit 'groups'."""
    if not MANUAL_PATH.exists():
        return {}
    import json as _json
    try:
        raw = _json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(raw, list):
        return {"Tags": [str(t).strip().lower() for t in raw if str(t).strip()]}
    if isinstance(raw, dict):
        groups = raw.get("groups", raw)  # erlaube Top-Level groups
        if isinstance(groups, dict):
            out = {}
            for name, tags in groups.items():
                if name.startswith("_"):
                    continue
                if isinstance(tags, list):
                    cleaned = [str(t).strip().lower() for t in tags if str(t).strip()]
                    if cleaned:
                        out[name] = cleaned
            return out
    return {}


CONTEXT_FILENAMES = (".mediasearch-context.txt", ".context.txt")
_CONTEXT_CACHE: dict[Path, str] = {}


def find_context(root: Path, file_path: Path) -> str:
    """Walk up from file's parent to root and return the first found context.
    Tag.py kann pro Ordner einen .mediasearch-context.txt anlegen (mit
    Hinweisen wie 'Hochzeitsbilder', 'Urlaub Italien 2023', etc.) -
    naechster gefundener gewinnt, Unterordner ueberschreiben Eltern."""
    root_r = root.resolve()
    current = file_path.parent.resolve()
    walked: list[Path] = []
    while True:
        if current in _CONTEXT_CACHE:
            ctx = _CONTEXT_CACHE[current]
            for p in walked:
                _CONTEXT_CACHE[p] = ctx
            return ctx
        for fname in CONTEXT_FILENAMES:
            cand = current / fname
            if cand.is_file():
                try:
                    ctx = cand.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    ctx = ""
                for p in walked + [current]:
                    _CONTEXT_CACHE[p] = ctx
                return ctx
        walked.append(current)
        if current == root_r or current.parent == current:
            for p in walked:
                _CONTEXT_CACHE[p] = ""
            return ""
        current = current.parent


def clear_context_cache() -> None:
    _CONTEXT_CACHE.clear()


def normalize_tags(tags: list[str], synonyms: dict[str, str] | None = None) -> list[str]:
    """Wendet die Synonym-Map an, dedupliziert und behaelt die Reihenfolge."""
    if synonyms is None:
        synonyms = load_synonyms()
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        t = t.strip().lower()
        if not t:
            continue
        canon = synonyms.get(t, t)
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def root_slot(root: Path) -> Path:
    """Pro Wurzelverzeichnis ein eigener Datenordner unter <scripts>/data/."""
    h = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    p = DATA_DIR / h
    p.mkdir(parents=True, exist_ok=True)
    (p / "thumbs").mkdir(exist_ok=True)
    # menschenlesbarer Hinweis welcher Pfad das war
    rt = p / "root.txt"
    if not rt.exists():
        rt.write_text(str(root) + "\n", encoding="utf-8")
    return p

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".heic", ".heif", ".avif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".m4v", ".mpg", ".mpeg", ".ts", ".3gp"}

DB_NAME = "mediasearch.db"
THUMB_SIZE = (240, 240)
THUMB_QUALITY = 72   # JPEG-Qualitaet 1-95


def db_path(root: Path) -> Path:
    return root_slot(root) / DB_NAME


def thumb_path(root: Path, file_id: int) -> Path:
    return root_slot(root) / "thumbs" / f"{file_id}.jpg"


def media_type(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


_NUMSEG_RE = re.compile(r"(\d+)")


def _natural_key(s: str | None) -> str:
    """Liefert einen Sortierschluessel, bei dem Zahlen auf Breite 10 aufgefuellt
    werden, damit '2' vor '10' kommt. Plus case-insensitive ueber casefold()."""
    if not s:
        return ""
    s = s.casefold()
    return _NUMSEG_RE.sub(lambda m: m.group(1).zfill(10), s)


def _natural_cmp(a: str, b: str) -> int:
    ka, kb = _natural_key(a), _natural_key(b)
    return (ka > kb) - (ka < kb)


def connect(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(root), timeout=30)
    conn.row_factory = sqlite3.Row
    # Performance-Pragmas: mehr Cache, mmap fuer schnelleren Read,
    # temp_store=MEMORY fuer Sortierungen ohne Disk-I/O
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")     # 64 MB Page-Cache
    conn.execute("PRAGMA mmap_size=268435456")   # 256 MB mmap
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    # Natural-Sort Collation fuer 'Pfad / Name' Sortierung:
    # case-insensitive, mit numerischer Reihenfolge bei Zahlen.
    # NICHT 'NATURAL' verwenden - das ist SQL-Reservewort (NATURAL JOIN).
    conn.create_collation("NATSORT", _natural_cmp)
    return conn


def optimize_db(root: Path) -> dict:
    """Wartung: WAL-Checkpoint, ANALYZE (Query-Planner-Stats),
    optional VACUUM (Defrag). Gibt Vorher/Nachher-Groessen zurueck."""
    p = db_path(root)
    before = p.stat().st_size if p.exists() else 0
    conn = connect(root)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("ANALYZE")
        # FTS5 hat einen optimize-Befehl der die Index-Segmente komprimiert
        try:
            conn.execute("INSERT INTO files_fts(files_fts) VALUES('optimize')")
        except Exception:
            pass
        conn.commit()
        # VACUUM muss ausserhalb einer Transaction laufen
        conn.isolation_level = None
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = p.stat().st_size if p.exists() else 0
    return {"size_before": before, "size_after": after,
            "saved": max(0, before - after)}


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path     TEXT UNIQUE NOT NULL,
    type         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    description  TEXT DEFAULT '',
    tags         TEXT DEFAULT '',
    manual_tags  TEXT DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT DEFAULT '',
    tagged_at    REAL DEFAULT 0,
    seen_at      REAL DEFAULT 0,
    started_at   REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_type ON files(type);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    description, tags, manual_tags, rel_path,
    content='files', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, description, tags, manual_tags, rel_path)
    VALUES (new.id, new.description, new.tags, new.manual_tags, new.rel_path);
END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, description, tags, manual_tags, rel_path)
    VALUES('delete', old.id, old.description, old.tags, old.manual_tags, old.rel_path);
END;
CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, description, tags, manual_tags, rel_path)
    VALUES('delete', old.id, old.description, old.tags, old.manual_tags, old.rel_path);
    INSERT INTO files_fts(rowid, description, tags, manual_tags, rel_path)
    VALUES (new.id, new.description, new.tags, new.manual_tags, new.rel_path);
END;
"""


_INIT_DONE: set[str] = set()


def init_db(root: Path, force: bool = False) -> None:
    """Idempotent. Wird pro Root-Pfad nur einmal pro Prozess wirklich ausgefuehrt."""
    key = str(root)
    if key in _INIT_DONE and not force:
        return
    conn = connect(root)
    try:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "started_at" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN started_at REAL DEFAULT 0")
        # Migration auf manual_tags + neues FTS-Schema
        if "manual_tags" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN manual_tags TEXT DEFAULT ''")
            conn.executescript("""
                DROP TRIGGER IF EXISTS files_ai;
                DROP TRIGGER IF EXISTS files_ad;
                DROP TRIGGER IF EXISTS files_au;
                DROP TABLE IF EXISTS files_fts;
            """)
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO files_fts(rowid, description, tags, manual_tags, rel_path) "
                "SELECT id, description, tags, manual_tags, rel_path FROM files"
            )
        conn.execute(
            "UPDATE files SET status='pending', started_at=0 "
            "WHERE status='processing'"
        )
        conn.commit()
    finally:
        conn.close()
    _INIT_DONE.add(key)


def iter_media(root: Path) -> Iterable[Path]:
    """Walk root, yield media files. (DB liegt nicht mehr im Root.)"""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if media_type(p) is not None:
                yield p


def video_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def extract_video_frame(path: Path, timestamp: float) -> bytes | None:
    """Return JPEG bytes of frame at given timestamp."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-ss", f"{timestamp:.2f}",
             "-i", str(path), "-frames:v", "1",
             "-vf", "scale='min(1024,iw)':-2",
             "-f", "image2", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=60,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout
    except Exception:
        pass
    return None


def video_frames_for_tagging(path: Path) -> list[bytes]:
    """Extract two frames: middle of first half, middle of second half."""
    dur = video_duration(path)
    if dur <= 0:
        # fallback: try at 0 and 1 seconds
        frames = [extract_video_frame(path, 0.0), extract_video_frame(path, 1.0)]
    else:
        frames = [extract_video_frame(path, dur * 0.25),
                  extract_video_frame(path, dur * 0.75)]
    return [f for f in frames if f]


def make_thumb_image(path: Path, dst: Path) -> bool:
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail(THUMB_SIZE)
            im.save(dst, "JPEG", quality=THUMB_QUALITY,
                    optimize=True, progressive=True)
        return True
    except Exception:
        return False


def make_thumb_video(path: Path, dst: Path) -> bool:
    dur = video_duration(path)
    ts = dur / 2 if dur > 0 else 0.5
    data = extract_video_frame(path, ts)
    if not data:
        return False
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail(THUMB_SIZE)
            im.save(dst, "JPEG", quality=THUMB_QUALITY,
                    optimize=True, progressive=True)
        return True
    except Exception:
        return False


def make_thumb(path: Path, kind: str, dst: Path) -> bool:
    if kind == "image":
        return make_thumb_image(path, dst)
    return make_thumb_video(path, dst)


def image_to_jpeg_bytes(path: Path, max_side: int = 1024) -> bytes | None:
    """Load any supported image, downscale, return JPEG bytes for LLM."""
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = min(1.0, max_side / max(w, h))
            if scale < 1.0:
                im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88)
            return buf.getvalue()
    except Exception:
        return None
