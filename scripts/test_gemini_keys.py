"""Test which Gemini API keys are active.

Sends a minimal text request to each key.

Usage:
    uv run python scripts/test_gemini_keys.py
"""

from __future__ import annotations

import os

from _utils import load_env


def main() -> None:
    load_env()
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
