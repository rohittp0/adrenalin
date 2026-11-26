import csv
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path


class EntityCollision(Exception):
    pass


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def entity_dir(out_root: Path, tool: str, entity_id: str, name: str) -> Path:
    d = out_root / tool / (slugify(name) or slugify(entity_id))
    marker = d / "entity.json"
    if marker.exists():
        existing = json.loads(marker.read_text())
        if existing.get("id") != entity_id:
            raise EntityCollision(
                f"{d} already belongs to {existing.get('id')!r}; cannot reuse it for {entity_id!r} ({name!r})")
        return d
    d.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"id": entity_id, "name": name}))
    return d


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _defuse(value: object) -> object:
    """Prefix a text cell a spreadsheet would run as a formula (CSV injection) with `'`. Numbers stay as is."""
    return "'" + value if isinstance(value, str) and value.startswith(_FORMULA_START) else value


def write_csv_atomic(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    count = 0
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _defuse(v) for k, v in row.items()})
                count += 1
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return count


def cache_dir(out_root: Path) -> Path:
    d = out_root / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: several writers can target one file (e.g. creatives sharing an image).
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
