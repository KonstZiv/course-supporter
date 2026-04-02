"""Test which Gemini API keys are active.

Sends a minimal text request to each key.

Usage:
    uv run python scripts/test_gemini_keys.py
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_env() -> None:
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


def main() -> None:
    _load_env()
    raw = os.environ.get("GEMINI_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]

    if not keys:
        print("No GEMINI_API_KEY found in .env")
        return

    print(f"Found {len(keys)} keys. Testing each...\n")

    import google.genai as genai

    working: list[int] = []
    broken: list[tuple[int, str]] = []

    for i, key in enumerate(keys, 1):
        masked = f"{key[:8]}...{key[-4:]}"
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents="Say OK",
            )
            text = (response.text or "").strip()[:50]
            print(f"  Key {i} ({masked}): OK — {text!r}")
            working.append(i)
        except Exception as e:
            err = str(e)[:80]
            print(f"  Key {i} ({masked}): FAIL — {err}")
            broken.append((i, err))

    print(f"\nWorking: {len(working)}/{len(keys)}")
    if broken:
        print("Broken keys:")
        for idx, err in broken:
            print(f"  Key {idx}: {err}")


if __name__ == "__main__":
    main()
