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


def _extract_code_blocks(text: str) -> str:
    """Extract content from fenced code blocks (```...```)."""
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


def _normalize_response(text: str) -> str:
    """Extract comparable text from a response.

    Handles JSON combined-prompt responses by extracting `extracted_text`.
    Falls back to extracting code blocks for OCR-only responses.
    """
    stripped = text.strip()

    # Strip ```json wrapper if present
    inner = stripped
    if inner.startswith("```"):
        lines = inner.splitlines()
        lines = lines[1:]  # remove opening ```json
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        inner = "\n".join(lines)

    # Try JSON parse → extract "extracted_text" field
    try:
        data = json.loads(inner)
        if isinstance(data, dict) and "extracted_text" in data:
            return data["extracted_text"]
    except (json.JSONDecodeError, ValueError):
        pass

    # Not JSON — extract code blocks (OCR-only prompt)
    blocks = _extract_code_blocks(text)
    return blocks if blocks else text


def compute_code_accuracy(extracted: str, ground_truth: str) -> float:
    """Compute character-level accuracy of code extraction.

    Normalizes both response and ground truth before comparing.
    """
    ex_text = _normalize_response(extracted)
    gt_text = _extract_code_blocks(ground_truth)  # GT always has ``` blocks

    if not gt_text.strip():
        return 1.0 if not ex_text.strip() else 0.0

    gt_chars = gt_text.strip()
    ex_chars = ex_text.strip()

    if not gt_chars:
        return 1.0

    # Greedy sequential character matching
    matches = 0
    gt_idx = 0
    ex_idx = 0

    while gt_idx < len(gt_chars) and ex_idx < len(ex_chars):
        if gt_chars[gt_idx] == ex_chars[ex_idx]:
            matches += 1
            gt_idx += 1
            ex_idx += 1
        else:
            ex_idx += 1

    return matches / len(gt_chars)


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
    model_id: str = "gemini-2.5-flash",
    max_retries_per_frame: int = 6,
    existing_results: list[dict] | None = None,
    on_frame_done: None | object = None,
) -> dict:
    """Test Gemini Vision on frames with key rotation and rate limit handling.

    Resumes from existing_results (skips already-done frames).
    Calls on_frame_done() after each successful frame for incremental save.
    Stops early if all keys are exhausted, returning partial results.
    """
    keys = _get_gemini_keys()
    if not keys:
        return {"error": "No GEMINI_API_KEY"}

    print(f"  Using {len(keys)} Gemini key(s)")

    # Resume: collect already-done frame names
    done_frames = {r["frame"] for r in (existing_results or [])}
    results: list[dict] = list(existing_results or [])
    if done_frames:
        print(f"  Resuming: {len(done_frames)} frames already done")

    key_idx = 0
    all_keys_exhausted = False
    total_input = sum(r.get("input_tokens", 0) for r in results)
    total_output = sum(r.get("output_tokens", 0) for r in results)

    for i, frame_path in enumerate(frames):
        if all_keys_exhausted:
            break
        if frame_path.name in done_frames:
            continue

        img_data = frame_path.read_bytes()
        frame_done = False

        for attempt in range(max_retries_per_frame):
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
                key_idx += 1
                frame_done = True
                print(
                    f"    [{i + 1}/{len(frames)}] {frame_path.name} OK ({latency:.1f}s)"
                )
                # Incremental save callback
                if on_frame_done is not None:
                    on_frame_done()  # type: ignore[operator]
                if i < len(frames) - 1:
                    time.sleep(13)  # 5 RPM limit → 12s+ between requests
                break

            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    key_idx += 1
                    if attempt >= len(keys) * 2 - 1:
                        print(
                            f"    All keys exhausted after {attempt + 1}"
                            " retries, stopping."
                        )
                        all_keys_exhausted = True
                        break
                    wait = 8 if attempt < len(keys) else 15
                    print(
                        f"    Rate limited (key {key_idx % len(keys)}),"
                        f" wait {wait}s... "
                        f"(attempt {attempt + 1}/{max_retries_per_frame})"
                    )
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
                    frame_done = True
                    break

        if not frame_done and not all_keys_exhausted:
            print(f"    [{i + 1}/{len(frames)}] {frame_path.name} FAILED (max retries)")

    n_ok = len([r for r in results if not r["response"].startswith("ERROR")])
    print(
        f"  Completed: {n_ok}/{len(frames)} frames"
        f"{' (partial — quota exhausted)' if all_keys_exhausted else ''}"
    )

    return {
        "model": model_id,
        "provider": "gemini",
        "results": results,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "partial": all_keys_exhausted,
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


def _should_run(all_results: dict, test_key: str) -> bool:
    """Check if a test should run: not done, or partial (quota exhausted)."""
    if test_key not in all_results:
        return True
    entry = all_results[test_key]
    if not entry.get("raw"):
        return True
    return bool(entry.get("partial"))


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
        for k, v in all_results.items():
            n = len(v.get("raw", []))
            partial = v.get("partial", False)
            status = f"{n} frames" + (" (PARTIAL)" if partial else "")
            print(f"  {k}: {status}")

    def _run_gemini_test(
        test_key: str,
        label: str,
        frames: list[Path],
        prompt: str,
        has_accuracy: bool = True,
    ) -> None:
        """Run a single Gemini test with frame-level resume and save."""
        if not _should_run(all_results, test_key):
            n = len(all_results[test_key].get("raw", []))
            avg = all_results[test_key].get("avg_accuracy", "")
            extra = f", avg={avg}%" if avg else ""
            print(f"\n  [SKIP] {label} done: {n} frames{extra}")
            return

        print("\n" + "─" * 70)
        print(f"{label}")
        print("─" * 70)

        existing_raw = all_results.get(test_key, {}).get("raw", [])

        def _save_after_frame() -> None:
            """Incremental save after each frame."""
            if has_accuracy:
                accs, avg = compute_accuracies({"raw": data["results"]})
                all_results[test_key] = {
                    "model": "gemini-2.5-flash",
                    "prompt": test_key.split("_", 2)[-1],
                    "accuracies": accs,
                    "avg_accuracy": avg,
                    "total_input_tokens": data["total_input_tokens"],
                    "total_output_tokens": data["total_output_tokens"],
                    "raw": data["results"],
                    "partial": True,  # still in progress
                }
            else:
                all_results[test_key] = {
                    "model": "gemini-2.5-flash",
                    "prompt": "describe_only",
                    "total_input_tokens": data["total_input_tokens"],
                    "total_output_tokens": data["total_output_tokens"],
                    "raw": data["results"],
                    "partial": True,
                }
            save_results(all_results)

        data = test_gemini(
            frames,
            prompt,
            "gemini-2.5-flash",
            existing_results=existing_raw,
            on_frame_done=_save_after_frame,
        )

        if "error" in data:
            all_results[test_key] = {"error": data["error"], "raw": []}
            print(f"  Error: {data['error']}")
        elif has_accuracy:
            accs, avg = compute_accuracies({"raw": data["results"]})
            all_results[test_key] = {
                "model": "gemini-2.5-flash",
                "prompt": test_key.split("_", 2)[-1],
                "accuracies": accs,
                "avg_accuracy": avg,
                "total_input_tokens": data["total_input_tokens"],
                "total_output_tokens": data["total_output_tokens"],
                "raw": data["results"],
                "partial": data.get("partial", False),
            }
            print(f"  Avg accuracy: {avg}% ({len(data['results'])} frames)")
        else:
            all_results[test_key] = {
                "model": "gemini-2.5-flash",
                "prompt": "describe_only",
                "total_input_tokens": data["total_input_tokens"],
                "total_output_tokens": data["total_output_tokens"],
                "raw": data["results"],
                "partial": data.get("partial", False),
            }
            for r in data["results"]:
                preview = r["response"][:100].replace("\n", " ")
                print(f"    {r['frame']}: {preview}...")
        save_results(all_results)

    # ── Test 1: Combined prompt — Gemini Flash ────────────────
    _run_gemini_test(
        "gemini_flash_combined",
        "TEST 1: Combined prompt — Gemini 2.5 Flash",
        gt_frame_paths,
        COMBINED_PROMPT,
    )

    # ── Test 2: Combined prompt — GPT-4o ──────────────────────
    test_key = "gpt4o_combined"
    if _should_run(all_results, test_key):
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
    _run_gemini_test(
        "gemini_flash_ocr",
        "TEST 3: OCR-only prompt — Gemini 2.5 Flash",
        gt_frame_paths,
        OCR_PROMPT,
    )

    # ── Test 4: Description-only — Gemini Flash ───────────────
    _run_gemini_test(
        "gemini_flash_describe",
        "TEST 4: Description-only — Gemini 2.5 Flash",
        frame_paths,
        DESCRIBE_PROMPT,
        has_accuracy=False,
    )

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
