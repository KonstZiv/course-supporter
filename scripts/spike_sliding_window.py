"""VD-SPIKE-B v2: Sliding Window Vision LLM testing.

Each step saves state; restarts resume from last checkpoint.

Steps (run in order):
  prepare  — scan golden frames, compute dHash, detect scenes, change scores
  windows  — generate adaptive sliding windows
  test     — run Vision LLM per window (1 model at a time)
  eval     — evaluate accuracy against ground truth
  summary  — compare models + single-frame vs window

Usage:
    uv run python scripts/spike_sliding_window.py
    uv run python scripts/spike_sliding_window.py --step test
    uv run python scripts/spike_sliding_window.py --step test --model gemini-3-flash
    uv run python scripts/spike_sliding_window.py --step eval
    uv run python scripts/spike_sliding_window.py --step summary
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path

import imagehash
from _utils import load_env
from PIL import Image

# ─── Constants ──────────────────────────────────────────────────────

GOLDEN_DIR = Path("current-doc/vd-spike/golden-frames-sample2")
GT_DIR = Path("current-doc/vd-spike/VD-SPIKE-B/ground-truth")
STATE_DIR = Path("current-doc/vd-spike/VD-SPIKE-B/sliding-window")

DHASH_SIZE = 16

# dHash distance thresholds (between consecutive golden frames)
DIST_LOW = 0.10  # <10% → "мало змін", step=3
DIST_HIGH = 0.20  # >20% → "значні зміни", scene boundary
# 10-20% → "є зміни", step=2

MAX_GAP_SEC = 10  # >10s between frames → scene boundary (for STT mapping)
WINDOW_MAX = 5  # max frames per window

MODELS: dict[str, dict] = {
    "gemini-2.5-flash": {"rpm": 5},
    "gemini-3-flash-preview": {"rpm": 5},
    "gemini-3.1-flash-lite-preview": {"rpm": 15},
    "gemini-2.5-flash-lite": {"rpm": 10},
}
DEFAULT_MODEL = "gemini-2.5-flash"

# ─── Logging ────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("spike-sw")

# ─── State management ──────────────────────────────────────────────


def _state_path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def load_state(name: str) -> dict | None:
    """Load a named state file. Returns None if not found."""
    path = _state_path(name)
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_state(name: str, data: dict) -> None:
    """Save a named state file (atomic-ish via write+rename)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(path)
    log.debug("Saved %s", path)


# ─── Env / API keys ────────────────────────────────────────────────


def _get_gemini_keys() -> list[str]:
    load_env()
    raw = os.environ.get("GEMINI_API_KEY", "")
    keys = list(dict.fromkeys(k.strip() for k in raw.split(",") if k.strip()))
    return keys


# ─── Frame parsing ──────────────────────────────────────────────────

_FRAME_RE = re.compile(r"golden_(\d+)_(\d+)s\.jpg")


def _parse_frame_name(name: str) -> tuple[int, int] | None:
    """Parse golden_NNN_Ts.jpg → (index, timestamp_sec)."""
    m = _FRAME_RE.match(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


# ═══════════════════════════════════════════════════════════════════
# STEP: prepare — scan, hash, segment, change scores (NO API calls)
# ═══════════════════════════════════════════════════════════════════


def step_prepare() -> dict:
    """Scan golden frames, compute dHash distances, group into scenes.

    Scene boundary = dHash distance > 20% between consecutive golden frames.
    Also classifies each pair: <10% "low", 10-20% "medium", >20% "boundary".
    """
    state = load_state("prepare")
    if state and state.get("complete"):
        nf = len(state["frames"])
        ns = len(state["scenes"])
        log.info("Prepare cached: %d frames, %d scenes", nf, ns)
        return state

    # 1. Scan and hash
    paths = sorted(GOLDEN_DIR.glob("golden_*.jpg"))
    log.info("Found %d golden frames in %s", len(paths), GOLDEN_DIR)

    frames: list[dict] = []
    for p in paths:
        parsed = _parse_frame_name(p.name)
        if not parsed:
            log.warning("Skipping %s — name doesn't match pattern", p.name)
            continue
        idx, ts = parsed
        img = Image.open(p)
        dhash = imagehash.dhash(img, hash_size=DHASH_SIZE)
        frames.append(
            {
                "name": p.name,
                "index": idx,
                "timestamp": ts,
                "dhash": str(dhash),
            }
        )

    log.info("Hashed %d frames", len(frames))

    # 2. Compute dHash distances and classify
    hash_bits = DHASH_SIZE * DHASH_SIZE
    frames[0]["dhash_dist"] = 0.0
    frames[0]["change_class"] = "first"

    for i in range(1, len(frames)):
        h1 = imagehash.hex_to_hash(frames[i - 1]["dhash"])
        h2 = imagehash.hex_to_hash(frames[i]["dhash"])
        dist = (h1 - h2) / hash_bits
        frames[i]["dhash_dist"] = round(dist, 4)

        time_gap = frames[i]["timestamp"] - frames[i - 1]["timestamp"]
        frames[i]["time_gap"] = time_gap

        if dist > DIST_HIGH or time_gap > MAX_GAP_SEC:
            frames[i]["change_class"] = "boundary"
        elif dist > DIST_LOW:
            frames[i]["change_class"] = "medium"
        else:
            frames[i]["change_class"] = "low"

    # 3. Build scenes (split at boundaries)
    scenes: list[dict] = []
    scene_start = 0
    for i in range(1, len(frames)):
        if frames[i]["change_class"] == "boundary":
            scenes.append(
                {
                    "id": len(scenes),
                    "start": scene_start,
                    "end": i - 1,
                    "count": i - scene_start,
                    "ts_range": [
                        frames[scene_start]["timestamp"],
                        frames[i - 1]["timestamp"],
                    ],
                }
            )
            scene_start = i
    scenes.append(
        {
            "id": len(scenes),
            "start": scene_start,
            "end": len(frames) - 1,
            "count": len(frames) - scene_start,
            "ts_range": [
                frames[scene_start]["timestamp"],
                frames[-1]["timestamp"],
            ],
        }
    )

    state = {"frames": frames, "scenes": scenes, "complete": True}
    save_state("prepare", state)

    # Summary
    n_low = sum(1 for f in frames if f.get("change_class") == "low")
    n_med = sum(1 for f in frames if f.get("change_class") == "medium")
    n_bnd = sum(1 for f in frames if f.get("change_class") == "boundary")
    log.info(
        "Change classes: %d low (<10%%), %d medium (10-20%%), %d boundary (>20%%)",
        n_low,
        n_med,
        n_bnd,
    )
    log.info("Segmented into %d scenes:", len(scenes))
    for s in scenes:
        frame_names = [frames[j]["name"] for j in range(s["start"], s["end"] + 1)]
        classes = [
            frames[j].get("change_class", "")
            for j in range(s["start"] + 1, s["end"] + 1)
        ]
        class_str = ", ".join(classes) if classes else "single"
        log.info(
            "  Scene %d: %d frames, ts %d–%ds [%s] (%s)",
            s["id"],
            s["count"],
            s["ts_range"][0],
            s["ts_range"][1],
            frame_names[0] + "…" + frame_names[-1]
            if len(frame_names) > 1
            else frame_names[0],
            class_str,
        )

    return state


# ═══════════════════════════════════════════════════════════════════
# STEP: windows — adaptive sliding windows (NO API calls)
# ═══════════════════════════════════════════════════════════════════


def step_windows(prep: dict) -> dict:
    """Generate adaptive sliding windows from scenes.

    Within a scene, step size depends on change_class of nearby frames:
      - all "low" (<10%)   → step=3
      - any "medium" (10-20%) → step=2
    Single-frame scenes get a window of size 1.
    """
    state = load_state("windows")
    if state and state.get("complete"):
        log.info("Windows cached: %d windows", len(state["windows"]))
        return state

    frames = prep["frames"]
    scenes = prep["scenes"]
    windows: list[dict] = []

    for scene in scenes:
        si, ei = scene["start"], scene["end"]
        scene_len = ei - si + 1

        if scene_len == 0:
            continue

        # Single-frame scene → single-frame window
        if scene_len == 1:
            windows.append(
                {
                    "id": len(windows),
                    "scene_id": scene["id"],
                    "anchor": si,
                    "indices": [si],
                    "names": [frames[si]["name"]],
                    "ts_range": [frames[si]["timestamp"], frames[si]["timestamp"]],
                    "size": 1,
                }
            )
            continue

        anchor = 0  # relative to scene start

        while anchor < scene_len:
            abs_anchor = si + anchor

            # Build window: up to WINDOW_MAX frames centered on anchor
            half = WINDOW_MAX // 2
            win_start = max(0, anchor - half)
            win_end = min(scene_len - 1, anchor + half)
            abs_start = si + win_start
            abs_end = si + win_end

            win_frames = frames[abs_start : abs_end + 1]

            windows.append(
                {
                    "id": len(windows),
                    "scene_id": scene["id"],
                    "anchor": abs_anchor,
                    "indices": list(range(abs_start, abs_end + 1)),
                    "names": [f["name"] for f in win_frames],
                    "ts_range": [
                        win_frames[0]["timestamp"],
                        win_frames[-1]["timestamp"],
                    ],
                    "size": len(win_frames),
                }
            )

            # Adaptive step based on change_class of nearby frames
            look_start = max(0, anchor + 1)
            look_end = min(scene_len - 1, anchor + 3)
            nearby_classes = [
                frames[si + j].get("change_class", "low")
                for j in range(look_start, look_end + 1)
            ]

            # noticeable changes → smaller step, else bigger
            step = 2 if any(c == "medium" for c in nearby_classes) else 3

            anchor += step

    # Mark windows that contain ground-truth frames (for eval)
    gt_frame_names: set[str] = set()
    for p in GT_DIR.glob("*.txt"):
        gt_frame_names.add(p.name.replace(".txt", ".jpg"))

    for w in windows:
        w["gt_frames"] = [n for n in w["names"] if n in gt_frame_names]

    n_with_gt = len([w for w in windows if w["gt_frames"]])

    state = {
        "windows": windows,
        "total_windows": len(windows),
        "windows_with_gt": n_with_gt,
        "complete": True,
    }
    save_state("windows", state)

    log.info("Generated %d windows (%d contain GT frames):", len(windows), n_with_gt)

    # Distribution
    sizes = [w["size"] for w in windows]
    n_single = sum(1 for s in sizes if s == 1)
    n_multi = sum(1 for s in sizes if s > 1)
    log.info(
        "  %d single-frame, %d multi-frame (avg size %.1f, max %d)",
        n_single,
        n_multi,
        sum(sizes) / len(sizes) if sizes else 0,
        max(sizes) if sizes else 0,
    )

    # Per-scene breakdown
    for scene in scenes:
        sw = [w for w in windows if w["scene_id"] == scene["id"]]
        if sw:
            gt_count = sum(len(w["gt_frames"]) for w in sw)
            log.info(
                "  Scene %d (%d frames → %d windows%s)",
                scene["id"],
                scene["count"],
                len(sw),
                f", {gt_count} GT" if gt_count else "",
            )

    return state


# ═══════════════════════════════════════════════════════════════════
# STEP: test — Vision LLM per window (API calls, incremental save)
# ═══════════════════════════════════════════════════════════════════

WINDOW_PROMPT = """\
You are analyzing a sequence of {n} consecutive frames from a Ukrainian Python \
programming course video.

These frames are from the SAME scene — they show the same content with minor \
changes (typing, scrolling, popups appearing/disappearing).

Synthesize information from ALL frames to produce a complete picture:

1. **scene_type**: one of [slide, code_editing, console, terminal, transition, \
talking_head]
2. **description** (Ukrainian): What is on screen, what the lecturer demonstrates.
3. **extracted_text**: ALL text/code visible across ANY of the frames:
   - If code is partially hidden by popup/overlay in one frame but visible in \
another — include it
   - Python code: preserve exact indentation, use ```python``` blocks
   - Console: include >>> prompts and output, use ```python``` blocks
   - Terminal: include tracebacks exactly, use ``` blocks
   - Slides: extract headings and bullet points
   - If multiple code areas visible: extract each separately
4. **has_code**: true/false
5. **importance**: 1-5 (5 = critical learning content)

Respond as JSON:
{{
  "scene_type": "...",
  "description": "...",
  "extracted_text": "...",
  "has_code": true,
  "importance": 3
}}
"""


def _model_state_name(model_id: str) -> str:
    return "test_" + model_id.replace(".", "_").replace("-", "_")


def step_test(win_state: dict, model_id: str) -> dict:
    """Run Vision LLM on each window. Saves after every window."""
    import google.genai as genai

    state_name = _model_state_name(model_id)
    state = load_state(state_name) or {
        "model": model_id,
        "results": {},
        "complete": False,
    }

    if state.get("complete"):
        n = len(state["results"])
        log.info("Test %s already complete: %d windows", model_id, n)
        return state

    keys = _get_gemini_keys()
    if not keys:
        log.error("No GEMINI_API_KEY in .env")
        return state

    log.info("Using %d Gemini key(s)", len(keys))

    windows = win_state["windows"]
    done = set(state["results"].keys())
    remaining = [w for w in windows if str(w["id"]) not in done]

    log.info(
        "Test %s: %d done, %d remaining (of %d total)",
        model_id,
        len(done),
        len(remaining),
        len(windows),
    )

    if not remaining:
        state["complete"] = True
        save_state(state_name, state)
        return state

    rpm = MODELS.get(model_id, {}).get("rpm", 5)
    wait_between = max(60.0 / rpm + 1.0, 5.0)
    log.info("Rate limit: %d RPM → %.0fs between requests", rpm, wait_between)

    key_idx = 0

    for w in remaining:
        wid = str(w["id"])

        # Load frame images
        parts = []
        for fname in w["names"]:
            img_path = GOLDEN_DIR / fname
            parts.append(
                genai.types.Part.from_bytes(
                    data=img_path.read_bytes(), mime_type="image/jpeg"
                )
            )
        parts.append(
            genai.types.Part.from_text(text=WINDOW_PROMPT.format(n=len(w["names"])))
        )

        # Try with key rotation
        success = False
        for attempt in range(len(keys) * 2):
            current_key = keys[key_idx % len(keys)]
            client = genai.Client(api_key=current_key)

            try:
                t0 = time.time()
                response = client.models.generate_content(
                    model=model_id,
                    contents=[genai.types.Content(parts=parts)],
                )
                latency = time.time() - t0

                text = response.text or ""
                usage = response.usage_metadata

                state["results"][wid] = {
                    "window_id": w["id"],
                    "names": w["names"],
                    "ts_range": w["ts_range"],
                    "response": text,
                    "latency_sec": round(latency, 2),
                    "input_tokens": (usage.prompt_token_count if usage else 0),
                    "output_tokens": (usage.candidates_token_count if usage else 0),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                save_state(state_name, state)

                key_idx += 1
                success = True

                log.info(
                    "  Window %s/%d OK (%.1fs, %d frames, %d+%d tok)",
                    wid,
                    len(windows) - 1,
                    latency,
                    w["size"],
                    state["results"][wid]["input_tokens"],
                    state["results"][wid]["output_tokens"],
                )

                # Respect rate limit
                time.sleep(wait_between)
                break

            except Exception as e:
                err = str(e)
                retryable = (
                    "429" in err
                    or "RESOURCE_EXHAUSTED" in err
                    or "503" in err
                    or "UNAVAILABLE" in err
                )
                if retryable:
                    key_idx += 1
                    if attempt >= len(keys) * 2 - 1:
                        log.error(
                            "All keys exhausted at window %s — stopping. "
                            "Re-run to resume.",
                            wid,
                        )
                        save_state(state_name, state)
                        return state
                    wait = 15 if attempt >= len(keys) else 8
                    reason = "503/unavailable" if "503" in err else "rate limited"
                    log.warning(
                        "  %s (key %d/%d), wait %ds (attempt %d/%d)",
                        reason,
                        key_idx % len(keys) + 1,
                        len(keys),
                        wait,
                        attempt + 1,
                        len(keys) * 2,
                    )
                    time.sleep(wait)
                elif "404" in err and "not found" in err.lower():
                    log.error(
                        "Model %s not found. Check model ID. Error: %s",
                        model_id,
                        err[:200],
                    )
                    return state
                else:
                    log.error("  Window %s error: %s", wid, err[:200])
                    state["results"][wid] = {
                        "window_id": w["id"],
                        "names": w["names"],
                        "error": err[:500],
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    save_state(state_name, state)
                    success = True
                    break

        if not success:
            log.error("  Window %s: all retries failed", wid)

    n_ok = len([r for r in state["results"].values() if "error" not in r])
    state["complete"] = len(state["results"]) >= len(windows)
    save_state(state_name, state)

    log.info(
        "Test %s finished: %d/%d OK, complete=%s",
        model_id,
        n_ok,
        len(windows),
        state["complete"],
    )
    return state


# ═══════════════════════════════════════════════════════════════════
# STEP: eval — accuracy against ground truth (NO API calls)
# ═══════════════════════════════════════════════════════════════════


def _extract_code_blocks(text: str) -> str:
    """Extract content from fenced code blocks."""
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
    """Extract comparable text from a Vision LLM response.

    Handles JSON (combined prompt) by extracting `extracted_text` field.
    Falls back to extracting code blocks.
    """
    stripped = text.strip()
    inner = stripped
    if inner.startswith("```"):
        lines = inner.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        inner = "\n".join(lines)

    try:
        data = json.loads(inner)
        if isinstance(data, dict) and "extracted_text" in data:
            return data["extracted_text"]
    except (json.JSONDecodeError, ValueError):
        pass

    blocks = _extract_code_blocks(text)
    return blocks if blocks else text


def _char_accuracy(extracted: str, ground_truth: str) -> float:
    """Greedy sequential character matching (recall-oriented)."""
    gt = ground_truth.strip()
    ex = extracted.strip()
    if not gt:
        return 1.0 if not ex else 0.0
    matches = 0
    gi = 0
    ei = 0
    while gi < len(gt) and ei < len(ex):
        if gt[gi] == ex[ei]:
            matches += 1
            gi += 1
            ei += 1
        else:
            ei += 1
    return matches / len(gt)


def step_eval(win_state: dict, model_id: str) -> dict:
    """Evaluate model results against ground truth."""
    state_name = _model_state_name(model_id)
    test_state = load_state(state_name)
    if not test_state or not test_state.get("results"):
        log.error("No test results for %s — run 'test' step first", model_id)
        return {}

    windows = win_state["windows"]
    results = test_state["results"]

    evals: list[dict] = []

    for w in windows:
        wid = str(w["id"])
        if wid not in results:
            continue
        r = results[wid]
        if "error" in r:
            continue
        if not w.get("gt_frames"):
            continue

        response_text = _normalize_response(r["response"])

        for gt_name in w["gt_frames"]:
            gt_path = GT_DIR / gt_name.replace(".jpg", ".txt")
            if not gt_path.exists():
                continue
            gt_text = _extract_code_blocks(gt_path.read_text())
            if not gt_text.strip():
                continue

            acc = _char_accuracy(response_text, gt_text)
            evals.append(
                {
                    "window_id": w["id"],
                    "gt_frame": gt_name,
                    "window_size": w["size"],
                    "accuracy": round(acc * 100, 1),
                    "gt_len": len(gt_text.strip()),
                    "response_len": len(response_text.strip()),
                }
            )

    avg = sum(e["accuracy"] for e in evals) / len(evals) if evals else 0

    eval_state = {
        "model": model_id,
        "evals": evals,
        "avg_accuracy": round(avg, 1),
        "n_evaluated": len(evals),
    }
    save_state(f"eval_{_model_state_name(model_id)}", eval_state)

    log.info("Eval %s: avg=%.1f%% (%d GT frames evaluated)", model_id, avg, len(evals))
    for e in evals:
        log.info(
            "  %s (window %d, %d frames): %.1f%%",
            e["gt_frame"],
            e["window_id"],
            e["window_size"],
            e["accuracy"],
        )

    return eval_state


# ═══════════════════════════════════════════════════════════════════
# STEP: summary — compare everything (NO API calls)
# ═══════════════════════════════════════════════════════════════════


def step_summary() -> None:
    """Print comparison of all tested models."""
    log.info("=" * 70)
    log.info("SUMMARY")
    log.info("=" * 70)

    # Load single-frame results from v1 spike for comparison
    v1_path = Path("current-doc/vd-spike/VD-SPIKE-B/results.json")
    v1: dict = {}
    if v1_path.exists():
        v1 = json.loads(v1_path.read_text())

    if v1:
        log.info("--- Single-frame baseline (v1 spike) ---")
        for key in ["gpt4o_combined", "gemini_flash_combined", "gemini_flash_ocr"]:
            entry = v1.get(key, {})
            avg = entry.get("avg_accuracy", 0)
            n = len(entry.get("raw", []))
            if n:
                log.info("  %s: avg=%.1f%% (%d frames)", key, avg, n)

    log.info("--- Sliding window results ---")
    for model_id in MODELS:
        sn = _model_state_name(model_id)
        eval_state = load_state(f"eval_{sn}")
        if not eval_state:
            continue
        avg = eval_state.get("avg_accuracy", 0)
        n = eval_state.get("n_evaluated", 0)
        log.info("  %s: avg=%.1f%% (%d GT frames)", model_id, avg, n)
        for e in eval_state.get("evals", []):
            log.info(
                "    %s: %.1f%% (window %d, %d frames)",
                e["gt_frame"],
                e["accuracy"],
                e["window_id"],
                e["window_size"],
            )

    log.info("=" * 70)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="VD-SPIKE-B v2: Sliding Window")
    parser.add_argument(
        "--step",
        choices=["prepare", "windows", "test", "eval", "summary", "all"],
        default="all",
        help="Which step to run (default: all local steps)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model for test/eval steps (default: {DEFAULT_MODEL})",
    )
    args = parser.parse_args()

    step = args.step
    model = args.model

    if step in ("all", "prepare"):
        prep = step_prepare()
    else:
        prep = load_state("prepare")
        if not prep:
            log.error("Run 'prepare' step first")
            return

    if step in ("all", "windows"):
        win = step_windows(prep)
    else:
        win = load_state("windows")
        if not win and step in ("test", "eval"):
            log.error("Run 'windows' step first")
            return

    if step == "test":
        if win:
            step_test(win, model)
        return

    if step == "eval":
        if win:
            step_eval(win, model)
        return

    if step == "summary":
        step_summary()
        return

    # "all" — run only local steps, print what to do next
    if win:
        script = "scripts/spike_sliding_window.py"
        log.info("")
        log.info("Local steps done. To run LLM tests:")
        for m in MODELS:
            log.info(
                "  uv run python %s --step test --model %s",
                script,
                m,
            )
        log.info("")
        log.info("Then evaluate:")
        log.info(
            "  uv run python %s --step eval --model <model>",
            script,
        )
        log.info("  uv run python %s --step summary", script)


if __name__ == "__main__":
    main()
