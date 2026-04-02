"""CP-2 prep: Compare 4 Vision LLM models on the same frames.

Picks 3 diverse frames from CP-1 videos, runs each model, saves
results as HTML for human comparison.

Usage:
    uv run python scripts/cp2_model_comparison.py
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import time
from base64 import b64encode
from pathlib import Path

MODELS = [
    "gemini-3.1-flash-lite-preview",
    "gemini-3.0-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

# Frames to test: pick diverse content types
TEST_FRAMES = [
    {
        "video": "video1_python_16min",
        "label": "Python code editor + highlighting",
        "frame_index": 240,  # ~480s (~8min), mid-video with code
    },
    {
        "video": "video2_style2_5min",
        "label": "Hardware / physical objects",
        "frame_index": 10,  # ~20s
    },
    {
        "video": "video3_style3_13min",
        "label": "Electronics assembly / diagram",
        "frame_index": 40,  # ~80s
    },
]

OUT_DIR = Path("tmp/cp2-compare")


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


def _get_keys() -> list[str]:
    _load_env()
    raw = os.environ.get("GEMINI_API_KEY", "")
    return list(dict.fromkeys(k.strip() for k in raw.split(",") if k.strip()))


def _get_frame_path(video_name: str, frame_index: int) -> Path:
    """Find the Nth frame in the raw extraction directory."""
    raw_dir = Path(f"tmp/cp1-test/{video_name}/frames/raw")
    frames = sorted(raw_dir.glob("frame_*.jpg"))
    if frame_index >= len(frames):
        frame_index = len(frames) - 1
    return frames[frame_index]


def _thumb_b64(img_path: Path, max_w: int = 400) -> str:
    import io

    from PIL import Image

    img = Image.open(img_path)
    ratio = max_w / img.width
    new_size = (max_w, int(img.height * ratio))
    img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return b64encode(buf.getvalue()).decode()


EYES_PROMPT = """\
You are analyzing a video frame. \
Describe EVERYTHING visible on the frame. \
This is raw data for further processing — be precise and complete.

Respond in Markdown:

## Scene Composition

**Setting:** <screen_recording | indoor | outdoor | studio | mixed>

**People:** For each person visible:
- Position in frame, appearance (brief), action, looking at what

**Elements:** Numbered list of ALL distinct visual areas:
- ID, type (text_area | code_area | image_or_diagram | ui_element | \
physical_object | environment), name, position on screen

## Element N: <name> (<position>)

For each element, provide ALL relevant fields based on its type:

- **text_area**: content_type, language, exact text
- **code_area**: code_type, language, exact code in fenced blocks
- **image_or_diagram**: what is depicted, labels/text
- **ui_element**: application name, key info
- **physical_object**: type, content if any
- **environment**: type, notable features

Rules:
- Extract ONLY what is visible. Do NOT infer or hallucinate.
- Preserve exact text: indentation, punctuation, special characters.
- If partially obscured, note "[partially hidden]".
- Use the language of the original content (do not translate).
- Code: wrap in fenced blocks with language tag if recognizable.
"""


async def call_model(
    model: str,
    image_path: Path,
    keys: list[str],
    key_idx: int,
) -> dict:
    """Call a single model with one image."""
    import google.genai as genai

    key = keys[key_idx % len(keys)]
    client = genai.Client(api_key=key)

    image_bytes = image_path.read_bytes()  # noqa: ASYNC240
    parts = [
        genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        genai.types.Part.from_text(text=EYES_PROMPT),
    ]

    t0 = time.time()
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=[genai.types.Content(parts=parts)],
        )
        latency = round(time.time() - t0, 2)
        usage = response.usage_metadata
        return {
            "model": model,
            "text": response.text or "",
            "latency_sec": latency,
            "input_tokens": usage.prompt_token_count if usage else 0,
            "output_tokens": usage.candidates_token_count if usage else 0,
            "error": None,
        }
    except Exception as e:
        return {
            "model": model,
            "text": "",
            "latency_sec": round(time.time() - t0, 2),
            "input_tokens": 0,
            "output_tokens": 0,
            "error": str(e)[:200],
        }


def generate_html(
    results: dict[str, dict[str, dict]],
    frame_paths: dict[str, Path],
    frame_labels: dict[str, str],
) -> str:
    """Generate comparison HTML."""
    parts: list[str] = []
    a = parts.append

    a("<!DOCTYPE html><html><head><meta charset='UTF-8'>")
    a("<title>CP-2: Model Comparison</title>")
    a("<style>")
    a("body{font-family:system-ui;margin:20px;background:#1a1a2e;color:#eee}")
    a("h2{color:#e94560}h3{color:#4ecca3}")
    a(".grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}")
    a(".card{background:#16213e;border-radius:8px;padding:12px}")
    a(".card h4{margin:0 0 8px;color:#e9a945}")
    a(".meta{font-size:12px;color:#888;margin-bottom:8px}")
    a(".response{font-size:13px;white-space:pre-wrap;")
    a("max-height:600px;overflow-y:auto;background:#0f3460;")
    a("padding:10px;border-radius:4px}")
    a("img{border-radius:4px;max-width:500px;display:block;")
    a("margin:10px 0}")
    a(".error{color:#e94560;font-weight:bold}")
    a("</style></head><body>")
    a("<h1>CP-2: Model Comparison (4 models x 3 frames)</h1>")

    for frame_key, label in frame_labels.items():
        fpath = frame_paths[frame_key]
        b64 = _thumb_b64(fpath, 500)

        a(f"<h2>{html.escape(label)}</h2>")
        a(f"<img src='data:image/jpeg;base64,{b64}'/>")
        a("<div class='grid'>")

        for model in MODELS:
            r = results[frame_key].get(model, {})
            a("<div class='card'>")
            a(f"<h4>{html.escape(model)}</h4>")

            if r.get("error"):
                a(f"<div class='error'>ERROR: {html.escape(r['error'])}</div>")
            else:
                lat = r.get("latency_sec", 0)
                tin = r.get("input_tokens", 0)
                tout = r.get("output_tokens", 0)
                a(f"<div class='meta'>{lat}s | in={tin} out={tout}</div>")
                resp = html.escape(r.get("text", ""))
                a(f"<div class='response'>{resp}</div>")

            a("</div>")

        a("</div>")

    a("</body></html>")
    return "".join(parts)


async def main() -> None:
    keys = _get_keys()
    if not keys:
        print("No GEMINI_API_KEY found")
        return

    print(f"Keys: {len(keys)}, Models: {len(MODELS)}")
    print(f"Frames: {len(TEST_FRAMES)}")
    print(f"Total API calls: {len(MODELS) * len(TEST_FRAMES)}")
    print()

    OUT_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

    # Resolve frame paths
    frame_paths: dict[str, Path] = {}
    frame_labels: dict[str, str] = {}
    for tf in TEST_FRAMES:
        key = tf["video"]
        frame_paths[key] = _get_frame_path(tf["video"], tf["frame_index"])
        frame_labels[key] = tf["label"]
        print(f"  {tf['label']}: {frame_paths[key].name}")

    # Run each model on each frame (sequential to respect rate limits)
    results: dict[str, dict[str, dict]] = {}
    call_idx = 0

    for tf in TEST_FRAMES:
        fkey = tf["video"]
        results[fkey] = {}
        fpath = frame_paths[fkey]

        for model in MODELS:
            print(f"\n  [{model}] {tf['label']}...", end=" ", flush=True)
            r = await call_model(model, fpath, keys, call_idx)
            call_idx += 1

            if r["error"]:
                print(f"ERROR: {r['error'][:60]}")
            else:
                print(f"OK ({r['latency_sec']}s, out={r['output_tokens']} tokens)")

            results[fkey][model] = r

            # Save individual result
            result_path = OUT_DIR / f"{fkey}_{model.replace('.', '_')}.json"
            result_path.write_text(json.dumps(r, indent=2, ensure_ascii=False))

            # Rate limit: wait between calls
            await asyncio.sleep(3)

    # Generate comparison HTML
    html_content = generate_html(results, frame_paths, frame_labels)
    html_path = OUT_DIR / "comparison.html"
    html_path.write_text(html_content)

    print(f"\n\nComparison: file://{html_path.resolve()}")

    # Summary table
    print("\n--- Summary ---")
    print(f"{'Model':<35} {'Latency':>8} {'In':>7} {'Out':>7} {'Errors':>6}")
    print("-" * 70)
    for model in MODELS:
        lats: list[float] = []
        tins: list[int] = []
        touts: list[int] = []
        errors = 0
        for fkey in results:
            r = results[fkey].get(model, {})
            if r.get("error"):
                errors += 1
            else:
                lats.append(r.get("latency_sec", 0))
                tins.append(r.get("input_tokens", 0))
                touts.append(r.get("output_tokens", 0))
        avg_lat = sum(lats) / len(lats) if lats else 0
        avg_in = sum(tins) // len(tins) if tins else 0
        avg_out = sum(touts) // len(touts) if touts else 0
        print(f"{model:<35} {avg_lat:>7.1f}s {avg_in:>7} {avg_out:>7} {errors:>6}")


if __name__ == "__main__":
    asyncio.run(main())
