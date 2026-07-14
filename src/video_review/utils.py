from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def safe_filename(value: str, fallback: str = "video") -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value.strip())
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def format_time(seconds: int | float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
