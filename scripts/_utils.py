"""Shared utilities for CP test scripts."""

from __future__ import annotations

import os
from base64 import b64encode
from pathlib import Path


def load_env() -> None:
    """Load .env file into os.environ (setdefault, no overwrite)."""
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def thumb_b64(img_path: Path, max_w: int = 500) -> str:
    """Create a JPEG thumbnail and return as base64 string."""
    import io

    from PIL import Image

    img = Image.open(img_path)
    ratio = max_w / img.width
    new_size = (max_w, int(img.height * ratio))
    img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return b64encode(buf.getvalue()).decode()


def find_frame(frame_dir: Path, filename: str) -> Path | None:
    """Locate a frame file in frame_dir or its subdirectories."""
    direct = frame_dir / filename
    if direct.exists():
        return direct
    for sub in frame_dir.iterdir():
        if sub.is_dir():
            candidate = sub / filename
            if candidate.exists():
                return candidate
    return None
