"""VD-SPIKE-B: Vision LLM spike — descriptions, OCR, combined prompt.

Tests Vision LLM models on golden frames from Spike A:
- Test 1: Scene descriptions (batch)
- Test 2: Vision LLM as OCR (code/text extraction vs ground truth)
- Test 3: Combined prompt (describe + extract in one request)

Usage:
    uv run python scripts/spike_vision_llm.py
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import google.genai as genai
import openai

GOLDEN_DIR = Path("current-doc/vd-spike/golden-frames-sample2")
GT_DIR = Path("current-doc/vd-spike/VD-SPIKE-B/ground-truth")
RESULTS_DIR = Path("current-doc/vd-spike/VD-SPIKE-B")

# Frames for testing (by index in golden set)
# Mix of scene types: slides, console, code+terminal
TEST_FRAMES = [
    "golden_000_0s.jpg",  # slide: title
    "golden_006_92s.jpg",  # slide+console: type()
    "golden_012_210s.jpg",  # slide+console: int()
    "golden_017_318s.jpg",  # slide+console: float, formatting
    "golden_027_988s.jpg",  # console only: arithmetic
    "golden_036_1063s.jpg",  # slide+console: round()
    "golden_042_1220s.jpg",  # slide+console: bool
    "golden_053_1314s.jpg",  # empty editor (new file)
    "golden_060_1366s.jpg",  # code+terminal: simple print
    "golden_065_1403s.jpg",  # code+terminal: type()
    "golden_075_1500s.jpg",  # code+terminal: traceback
    "golden_080_1524s.jpg",  # code+terminal: two inputs
    "golden_085_1549s.jpg",  # code+terminal: float error
    "golden_090_1579s.jpg",  # slide: homework
]

# Frames with ground truth for OCR accuracy test
GT_FRAMES = [
    "golden_006_92s.jpg",
    "golden_012_210s.jpg",
    "golden_017_318s.jpg",
    "golden_027_988s.jpg",
    "golden_042_1220s.jpg",
    "golden_060_1366s.jpg",
    "golden_065_1403s.jpg",
    "golden_075_1500s.jpg",
    "golden_080_1524s.jpg",
    "golden_085_1549s.jpg",
]

DESCRIBE_PROMPT = """\
You are analyzing frames from a Ukrainian Python programming course video.
For each frame, provide:
1. scene_type: one of [slide, code_editing, console, terminal, transition, talking_head]
2. short_description: 1-2 sentences about what's on screen (Ukrainian)
3. has_text_content: true/false
4. importance: 1-5 (5 = critical content, 1 = can skip)

Respond as JSON array.
"""

OCR_PROMPT = """\
Extract ALL text and code visible on screen. Be precise:
- For Python code: preserve exact indentation, underscores, arrows (->)
- For console: include >>> prompts and output
- For terminal: include command output and tracebacks
- For slides: extract headings and bullet points
- Wrap code in ```python``` blocks, terminal in `````` blocks
- If multiple code areas visible, extract each separately
- Extract ONLY what is visible, do not infer or complete code
"""

COMBINED_PROMPT = """\
You are analyzing a frame from a Ukrainian Python programming course video.

Provide TWO things:

1. **description** (Ukrainian): What is on screen — scene type, what content
   is shown, what the lecturer is demonstrating.

2. **extracted_text**: ALL text/code visible on screen. Be precise:
   - Python code: preserve exact indentation, underscores, arrows
   - Console: include >>> prompts and output
   - Terminal: include tracebacks exactly
   - Slides: headings and bullet points
   - Wrap code in ```python``` blocks
   - If multiple code areas: extract each separately

Respond as JSON:
{
  "scene_type": "slide|code_editing|console|terminal|transition",
  "description": "...",
  "extracted_text": "...",
  "has_code": true/false,
  "importance": 1-5
}
"""


def load_image_base64(path: Path) -> str:
    """Load image and encode as base64."""
    return base64.b64encode(path.read_bytes()).decode()


def load_ground_truth(frame_name: str) -> str:
    """Load ground truth text for a frame."""
    gt_name = frame_name.replace(".jpg", ".txt")
    gt_path = GT_DIR / gt_name
    if gt_path.exists():
        return gt_path.read_text()
    return ""


def compute_code_accuracy(extracted: str, ground_truth: str) -> float:
    """Compute character-level accuracy of code extraction.

    Extracts code blocks from both texts and compares.
    """

    def extract_code_blocks(text: str) -> str:
        blocks: list[str] = []
        in_block = False
        current: list[str] = []
        for line in text.splitlines():
            if line.strip().startswith("```"):
                if in_block:
                    blocks.append("\n".join(current))
                    current = []
                    in_block = False
                else:
                    in_block = True
            elif in_block:
                current.append(line)
        if current:
            blocks.append("\n".join(current))
        return "\n---\n".join(blocks)

    gt_code = extract_code_blocks(ground_truth)
    ex_code = extract_code_blocks(extracted)

    if not gt_code:
        return 1.0 if not ex_code else 0.0

    # Character-level similarity (simple)
    gt_chars = gt_code.strip()
    ex_chars = ex_code.strip()

    if not gt_chars:
        return 1.0

    # Use longest common subsequence ratio
    matches = 0
    gt_idx = 0
    ex_idx = 0
    gt_len = len(gt_chars)
    ex_len = len(ex_chars)

    # Simple sequential matching
    while gt_idx < gt_len and ex_idx < ex_len:
        if gt_chars[gt_idx] == ex_chars[ex_idx]:
            matches += 1
            gt_idx += 1
            ex_idx += 1
        else:
            ex_idx += 1

    return matches / gt_len if gt_len > 0 else 1.0


# ─── Gemini ──────────────────────────────────────────────────────────


def _load_env() -> None:
    """Load .env file into environment."""
    import os

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


def _get_gemini_keys() -> list[str]:
    """Get all Gemini API keys from env (comma-separated rotation)."""
    import os

    _load_env()
    raw = os.environ.get("GEMINI_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys


def test_gemini(
    frames: list[Path],
    prompt: str,
    model_id: str = "gemini-2.0-flash",
) -> dict:
    """Test Gemini Vision on frames with key rotation and rate limit handling."""
    keys = _get_gemini_keys()
    if not keys:
        return {"error": "No GEMINI_API_KEY"}

    print(f"  Using {len(keys)} Gemini key(s)")
    key_idx = 0

    results: list[dict] = []
    total_input = 0
    total_output = 0

    for i, frame_path in enumerate(frames):
        img_data = frame_path.read_bytes()

        # Try each key, with retries on rate limit
        for attempt in range(len(keys) * 2):
            current_key = keys[key_idx % len(keys)]
            client = genai.Client(api_key=current_key)

            try:
                t0 = time.time()
                response = client.models.generate_content(
                    model=model_id,
                    contents=[
                        genai.types.Content(
                            parts=[
                                genai.types.Part.from_bytes(
                                    data=img_data,
                                    mime_type="image/jpeg",
                                ),
                                genai.types.Part.from_text(text=prompt),
                            ],
                        ),
                    ],
                )
                latency = time.time() - t0

                text = response.text or ""
                usage = response.usage_metadata
                in_tok = usage.prompt_token_count if usage else 0
                out_tok = usage.candidates_token_count if usage else 0
                total_input += in_tok
                total_output += out_tok

                results.append(
                    {
                        "frame": frame_path.name,
                        "response": text,
                        "latency_sec": round(latency, 2),
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                    }
                )
                # Rotate key after each successful call
                key_idx += 1
                # Small pause between requests
                if i < len(frames) - 1:
                    time.sleep(2)
                break

            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    key_idx += 1
                    wait = 10 if attempt < len(keys) else 30
                    print(f"    Rate limited, rotating key, wait {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"    Error: {e}")
                    results.append(
                        {
                            "frame": frame_path.name,
                            "response": f"ERROR: {e}",
                            "latency_sec": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                        }
                    )
                    break

    return {
        "model": model_id,
        "provider": "gemini",
        "results": results,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
    }


# ─── OpenAI (GPT-4o) ────────────────────────────────────────────────


def test_openai(
    frames: list[Path],
    prompt: str,
    model_id: str = "gpt-4o",
) -> dict:
    """Test OpenAI Vision on frames."""
    import os

    _load_env()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {"error": "No OPENAI_API_KEY"}

    client = openai.OpenAI(api_key=api_key)

    results: list[dict] = []
    total_input = 0
    total_output = 0

    for frame_path in frames:
        b64 = load_image_base64(frame_path)

        t0 = time.time()
        response = client.chat.completions.create(
            model=model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}",
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            max_tokens=2000,
        )
        latency = time.time() - t0

        text = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        total_input += input_tokens
        total_output += output_tokens

        results.append(
            {
                "frame": frame_path.name,
                "response": text,
                "latency_sec": round(latency, 2),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )

    return {
        "model": model_id,
        "provider": "openai",
        "results": results,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
    }


# ─── Incremental results store ───────────────────────────────────────

RESULTS_FILE = RESULTS_DIR / "results.json"


def load_results() -> dict:
    """Load previously saved results (incremental resume)."""
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {}


def save_results(all_results: dict) -> None:
    """Save results incrementally after each test."""
    RESULTS_FILE.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"  [saved to {RESULTS_FILE}]")


def compute_accuracies(test_data: dict) -> tuple[list[dict], float]:
    """Compute code accuracy for a test's raw results."""
    accs: list[dict] = []
    for r in test_data.get("raw", []):
        gt = load_ground_truth(r["frame"])
        if gt:
            acc = compute_code_accuracy(r["response"], gt)
            accs.append(
                {
                    "frame": r["frame"],
                    "accuracy": round(acc * 100, 1),
                    "latency": r["latency_sec"],
                }
            )
    avg = sum(a["accuracy"] for a in accs) / len(accs) if accs else 0
    return accs, round(avg, 1)


# ─── Main ────────────────────────────────────────────────────────────


def main() -> None:
    print("=" * 70)
    print("VD-SPIKE-B: Vision LLM Testing (incremental)")
    print("=" * 70)

    frame_paths = [GOLDEN_DIR / f for f in TEST_FRAMES]
    gt_frame_paths = [GOLDEN_DIR / f for f in GT_FRAMES]

    for p in frame_paths:
        assert p.exists(), f"Frame not found: {p}"

    all_results = load_results()
    if all_results:
        done = [k for k, v in all_results.items() if v.get("raw")]
        print(f"  Resuming: {len(done)} tests already done: {done}")

    # ── Test 1: Combined prompt — Gemini Flash ────────────────
    test_key = "gemini_flash_combined"
    if test_key not in all_results or not all_results[test_key].get("raw"):
        print("\n" + "─" * 70)
        print("TEST 1: Combined prompt — Gemini Flash")
        print("─" * 70)

        data = test_gemini(gt_frame_paths, COMBINED_PROMPT, "gemini-2.0-flash")
        if "error" not in data:
            accs, avg = compute_accuracies({"raw": data["results"]})
            all_results[test_key] = {
                "model": "gemini-2.0-flash",
                "prompt": "combined",
                "accuracies": accs,
                "avg_accuracy": avg,
                "total_input_tokens": data["total_input_tokens"],
                "total_output_tokens": data["total_output_tokens"],
                "raw": data["results"],
            }
            print(f"  Avg accuracy: {avg}%")
        else:
            all_results[test_key] = {"error": data["error"], "raw": []}
            print(f"  Error: {data['error']}")
        save_results(all_results)
    else:
        avg = all_results[test_key].get("avg_accuracy", 0)
        print(f"\n  [SKIP] Test 1 already done: avg={avg}%")

    # ── Test 2: Combined prompt — GPT-4o ──────────────────────
    test_key = "gpt4o_combined"
    if test_key not in all_results or not all_results[test_key].get("raw"):
        print("\n" + "─" * 70)
        print("TEST 2: Combined prompt — GPT-4o")
        print("─" * 70)

        data = test_openai(gt_frame_paths, COMBINED_PROMPT, "gpt-4o")
        if "error" not in data:
            accs, avg = compute_accuracies({"raw": data["results"]})
            all_results[test_key] = {
                "model": "gpt-4o",
                "prompt": "combined",
                "accuracies": accs,
                "avg_accuracy": avg,
                "total_input_tokens": data["total_input_tokens"],
                "total_output_tokens": data["total_output_tokens"],
                "raw": data["results"],
            }
            print(f"  Avg accuracy: {avg}%")
        else:
            all_results[test_key] = {"error": data["error"], "raw": []}
            print(f"  Error: {data['error']}")
        save_results(all_results)
    else:
        avg = all_results[test_key].get("avg_accuracy", 0)
        print(f"\n  [SKIP] Test 2 already done: avg={avg}%")

    # ── Test 3: OCR-only prompt — Gemini Flash ────────────────
    test_key = "gemini_flash_ocr"
    if test_key not in all_results or not all_results[test_key].get("raw"):
        print("\n" + "─" * 70)
        print("TEST 3: OCR-only prompt — Gemini Flash")
        print("─" * 70)

        data = test_gemini(gt_frame_paths, OCR_PROMPT, "gemini-2.0-flash")
        if "error" not in data:
            accs, avg = compute_accuracies({"raw": data["results"]})
            all_results[test_key] = {
                "model": "gemini-2.0-flash",
                "prompt": "ocr_only",
                "accuracies": accs,
                "avg_accuracy": avg,
                "total_input_tokens": data["total_input_tokens"],
                "total_output_tokens": data["total_output_tokens"],
                "raw": data["results"],
            }
            print(f"  Avg accuracy: {avg}%")
        else:
            all_results[test_key] = {"error": data["error"], "raw": []}
            print(f"  Error: {data['error']}")
        save_results(all_results)
    else:
        avg = all_results[test_key].get("avg_accuracy", 0)
        print(f"\n  [SKIP] Test 3 already done: avg={avg}%")

    # ── Test 4: Description-only — Gemini Flash ───────────────
    test_key = "gemini_flash_describe"
    if test_key not in all_results or not all_results[test_key].get("raw"):
        print("\n" + "─" * 70)
        print("TEST 4: Description-only — Gemini Flash")
        print("─" * 70)

        data = test_gemini(frame_paths, DESCRIBE_PROMPT, "gemini-2.0-flash")
        if "error" not in data:
            all_results[test_key] = {
                "model": "gemini-2.0-flash",
                "prompt": "describe_only",
                "total_input_tokens": data["total_input_tokens"],
                "total_output_tokens": data["total_output_tokens"],
                "raw": data["results"],
            }
            for r in data["results"]:
                preview = r["response"][:100].replace("\n", " ")
                print(f"    {r['frame']}: {preview}...")
        else:
            all_results[test_key] = {"error": data["error"], "raw": []}
            print(f"  Error: {data['error']}")
        save_results(all_results)
    else:
        n = len(all_results[test_key].get("raw", []))
        print(f"\n  [SKIP] Test 4 already done: {n} frames")

    # ── Summary ───────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for key, label in [
        ("gemini_flash_combined", "Gemini Flash combined"),
        ("gpt4o_combined", "GPT-4o combined"),
        ("gemini_flash_ocr", "Gemini Flash OCR-only"),
    ]:
        entry = all_results.get(key, {})
        avg = entry.get("avg_accuracy", 0)
        n = len(entry.get("raw", []))
        status = f"avg={avg}%" if n > 0 else "NOT RUN"
        print(f"  {label}: {status} ({n} frames)")

    desc = all_results.get("gemini_flash_describe", {})
    desc_n = len(desc.get("raw", []))
    print(f"  Gemini Flash describe: {desc_n} frames")

    # Stage C decision
    gem_avg = all_results.get("gemini_flash_combined", {}).get("avg_accuracy", 0)
    gpt_avg = all_results.get("gpt4o_combined", {}).get("avg_accuracy", 0)
    best = max(gem_avg, gpt_avg)

    print()
    if best >= 90:
        print(f"  DECISION: Stage C NOT NEEDED (best={best}%)")
    elif best >= 70:
        print(f"  DECISION: Stage C as fallback (best={best}%)")
    elif best > 0:
        print(f"  DECISION: Stage C REQUIRED (best={best}%)")
    else:
        print("  DECISION: Pending (no successful tests yet)")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
