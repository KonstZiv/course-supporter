"""VD-SPIKE-B v3 multi-model: same algorithm, model-namespaced state files.

Shares frames.json with spike_vd_pipeline.py but stores Eyes/Memory results
per model (e.g. eyes_gemini_3_1_flash_lite_preview.json). Each model resumes
independently.

Usage:
    uv run python scripts/spike_vd_multimodel.py --model gemini-3.1-flash-lite-preview
    uv run python scripts/spike_vd_multimodel.py --model gemini-2.5-flash-lite
    uv run python scripts/spike_vd_multimodel.py --step eval \
        --model gemini-3.1-flash-lite-preview
    uv run python scripts/spike_vd_multimodel.py --step summary  # compare all models
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import imagehash
from _utils import load_env
from PIL import Image

# ─── Constants ──────────────────────────────────────────────────────

GOLDEN_DIR = Path("current-doc/vd-spike/golden-frames-sample2")
MANIFEST = GOLDEN_DIR / "manifest.json"
VIDEO_PATH = Path("current-doc/vd-spike/sample2.mp4")
GT_DIR = Path("current-doc/vd-spike/VD-SPIKE-B/ground-truth")
STATE_DIR = Path("current-doc/vd-spike/VD-SPIKE-B/pipeline")
GAP_FRAMES_DIR = STATE_DIR / "gap_frames"

DHASH_SIZE = 16
HASH_BITS = DHASH_SIZE * DHASH_SIZE
DIST_BOUNDARY = 0.20  # >20% dHash → scene boundary
MAX_GAP_SEC = 10  # >10s → scene boundary
GAP_FILL_THRESHOLD = 15.0  # gaps >15s get filled
GAP_FILL_INTERVAL = 15.0  # target interval for fill frames
CONTEXT_IMG_MAX_GAP = 7.0  # previous images within 7s only

VISION_MODEL = "gemini-2.5-flash"
TEXT_MODEL = "gemini-3.1-flash-lite-preview"

# ─── Logging ────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vd-pipe")

# ─── Utilities ──────────────────────────────────────────────────────


def _get_gemini_keys() -> list[str]:
    load_env()
    raw = os.environ.get("GEMINI_API_KEY", "")
    return list(dict.fromkeys(k.strip() for k in raw.split(",") if k.strip()))


def _state_path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def load_state(name: str) -> dict | None:
    path = _state_path(name)
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_state(name: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(path)


_FRAME_RE = re.compile(r"(golden|fill)_(\d+)_(\d+)s\.jpg")


def _model_slug(model_id: str) -> str:
    """Convert model ID to filesystem-safe slug."""
    return model_id.replace(".", "_").replace("-", "_")


def load_model_state(name: str, model_id: str) -> dict | None:
    """Load model-namespaced state file."""
    slug = _model_slug(model_id)
    return load_state(f"{name}_{slug}")


def save_model_state(name: str, model_id: str, data: dict) -> None:
    """Save model-namespaced state file."""
    slug = _model_slug(model_id)
    save_state(f"{name}_{slug}", data)


def _frame_dir(frame: dict) -> Path:
    """Return directory containing this frame's image."""
    if frame["source"] == "golden":
        return GOLDEN_DIR
    return GAP_FRAMES_DIR


# ─── Gemini API pool ───────────────────────────────────────────────


class GeminiPool:
    """Key rotation + rate limiting for Gemini API."""

    def __init__(self, keys: list[str], rpm: int = 5) -> None:
        import google.genai as genai

        self._genai = genai
        self.keys = keys
        self.rpm = rpm
        self.key_idx = 0
        self.wait_sec = 60.0 / rpm + 1.0
        self.last_call = 0.0

    def call(
        self,
        model: str,
        parts: list,
        max_retries: int = 8,
    ) -> dict:
        """Make API call with key rotation, rate limiting, retries."""
        for attempt in range(max_retries):
            elapsed = time.time() - self.last_call
            if elapsed < self.wait_sec:
                time.sleep(self.wait_sec - elapsed)

            key = self.keys[self.key_idx % len(self.keys)]
            client = self._genai.Client(api_key=key)

            try:
                t0 = time.time()
                response = client.models.generate_content(
                    model=model,
                    contents=[self._genai.types.Content(parts=parts)],
                )
                self.last_call = time.time()
                self.key_idx += 1

                usage = response.usage_metadata
                return {
                    "text": response.text or "",
                    "input_tokens": usage.prompt_token_count if usage else 0,
                    "output_tokens": usage.candidates_token_count if usage else 0,
                    "latency_sec": round(time.time() - t0, 2),
                }

            except Exception as e:
                err = str(e)
                retryable = (
                    "429" in err
                    or "RESOURCE_EXHAUSTED" in err
                    or "503" in err
                    or "UNAVAILABLE" in err
                )
                if retryable:
                    self.key_idx += 1
                    if attempt >= len(self.keys) * 2 - 1:
                        log.error("All keys exhausted — stopping. Re-run to resume.")
                        raise
                    wait = 8 if attempt < len(self.keys) else 20
                    reason = "503" if "503" in err else "429"
                    log.warning(
                        "  %s (key %d/%d), wait %ds (attempt %d/%d)",
                        reason,
                        self.key_idx % len(self.keys) + 1,
                        len(self.keys),
                        wait,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(wait)
                else:
                    raise

        raise RuntimeError(f"All {max_retries} retries exhausted")


# ═══════════════════════════════════════════════════════════════════
# STEP: frames — golden + gap fill + scene segmentation (NO API)
# ═══════════════════════════════════════════════════════════════════


def _extract_frame_ffmpeg(video: Path, ts: float, output: Path) -> bool:
    """Extract single frame at timestamp via ffmpeg."""
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{ts:.2f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return result.returncode == 0 and output.exists()


def step_frames() -> dict:
    """Build dense frame set: golden + gap-fill, with scene segmentation."""
    state = load_state("frames")
    if state and state.get("complete"):
        nf = len(state["frames"])
        ns = len(state["scenes"])
        log.info("Frames cached: %d frames, %d scenes", nf, ns)
        return state

    # 1. Load golden manifest
    manifest = json.loads(MANIFEST.read_text())
    log.info("Loaded %d golden frames from manifest", len(manifest))

    frames: list[dict] = []
    for entry in manifest:
        frames.append(
            {
                "frame_id": entry["filename"].replace(".jpg", ""),
                "filename": entry["filename"],
                "timestamp": entry["timestamp_sec"],
                "source": "golden",
            }
        )

    # 2. Gap filling
    fill_count = 0
    new_fills: list[dict] = []
    for i in range(len(frames) - 1):
        gap = frames[i + 1]["timestamp"] - frames[i]["timestamp"]
        if gap > GAP_FILL_THRESHOLD:
            n_fill = max(1, round(gap / GAP_FILL_INTERVAL) - 1)
            interval = gap / (n_fill + 1)
            for j in range(1, n_fill + 1):
                ts = frames[i]["timestamp"] + interval * j
                ts_int = round(ts)
                fname = f"fill_{fill_count:03d}_{ts_int}s.jpg"
                fpath = GAP_FRAMES_DIR / fname

                if not fpath.exists():
                    ok = _extract_frame_ffmpeg(VIDEO_PATH, ts, fpath)
                    if not ok:
                        log.warning("  ffmpeg failed for ts=%.1f", ts)
                        continue
                    log.info("  Extracted gap frame: %s (%.1fs)", fname, ts)
                else:
                    log.info("  Gap frame cached: %s", fname)

                new_fills.append(
                    {
                        "frame_id": fname.replace(".jpg", ""),
                        "filename": fname,
                        "timestamp": round(ts, 1),
                        "source": "gap_fill",
                    }
                )
                fill_count += 1

    frames.extend(new_fills)
    frames.sort(key=lambda f: f["timestamp"])
    log.info(
        "Dense set: %d frames (%d golden + %d fill)",
        len(frames),
        len(manifest),
        fill_count,
    )

    # 3. Compute dHash for all frames
    for f in frames:
        img_path = _frame_dir(f) / f["filename"]
        img = Image.open(img_path)
        dhash = imagehash.dhash(img, hash_size=DHASH_SIZE)
        f["dhash"] = str(dhash)

    # 4. Distances + scene segmentation
    frames[0]["dhash_dist"] = 0.0
    frames[0]["time_gap"] = 0.0
    frames[0]["change_class"] = "first"

    for i in range(1, len(frames)):
        h1 = imagehash.hex_to_hash(frames[i - 1]["dhash"])
        h2 = imagehash.hex_to_hash(frames[i]["dhash"])
        dist = (h1 - h2) / HASH_BITS
        gap = frames[i]["timestamp"] - frames[i - 1]["timestamp"]
        frames[i]["dhash_dist"] = round(dist, 4)
        frames[i]["time_gap"] = round(gap, 1)

        if dist > DIST_BOUNDARY or gap > MAX_GAP_SEC:
            frames[i]["change_class"] = "boundary"
        elif dist > 0.10:
            frames[i]["change_class"] = "medium"
        else:
            frames[i]["change_class"] = "low"

    # 5. Build scenes
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
            "ts_range": [frames[scene_start]["timestamp"], frames[-1]["timestamp"]],
        }
    )

    # Mark GT frames
    gt_names = {p.name.replace(".txt", ".jpg") for p in GT_DIR.glob("*.txt")}
    for f in frames:
        f["has_gt"] = f["filename"] in gt_names

    state = {"frames": frames, "scenes": scenes, "complete": True}
    save_state("frames", state)

    n_gt = sum(1 for f in frames if f["has_gt"])
    log.info("Segmented into %d scenes, %d frames have GT", len(scenes), n_gt)
    for s in scenes:
        log.info(
            "  Scene %d: %d frames, ts %.0f–%.0fs",
            s["id"],
            s["count"],
            s["ts_range"][0],
            s["ts_range"][1],
        )

    return state


# ═══════════════════════════════════════════════════════════════════
# STEP: pipeline — Eyes + Memory, scene by scene (API calls)
# ═══════════════════════════════════════════════════════════════════

EYES_PROMPT = """\
You are analyzing a video frame. The LAST image is the MAIN frame to describe. \
Previous images (if any) are context — use them if something on the MAIN frame \
is occluded, has artifacts, or is low quality.

{course_context_block}\
{scene_context_block}\
Describe EVERYTHING visible on the MAIN frame. This is raw data for further \
processing — be precise and complete.

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

- **text_area** (slide, poster, whiteboard, document, sign): \
content_type (heading/body/handwriting/printed), language, exact text
- **code_area** (editor, console, terminal): \
code_type (source/repl/output), language if recognizable, \
exact code in fenced blocks (```python```, ```bash```, or plain ```)
- **image_or_diagram** (chart, schema, photo): what is depicted, labels/text
- **ui_element** (sidebar, toolbar, status bar, menu): application name, key info
- **physical_object** (whiteboard, projector screen, desk): type, content if any
- **environment** (background, room): type, notable features

Rules:
- Extract ONLY what is visible. Do NOT infer, complete, or hallucinate.
- Preserve exact text: indentation, punctuation, special characters.
- If partially obscured, extract what IS visible and note "[partially hidden]".
- Use the language of the original content (do not translate).
- Code: wrap in fenced blocks with language tag if recognizable.
"""

INSTANT_MERGE_PROMPT = """\
Below are text/code extractions from {n} consecutive frames of the same scene \
in a Python programming video. Frames show the same screen with small changes \
(typing, scrolling, output appearing).

Merge into ONE complete text containing ALL code/text visible across all frames. \
Where frames overlap (same code with additions), produce the most complete version. \
Preserve exact Python formatting.

{frame_texts}

Respond with the merged text only. No JSON, no explanation.
"""

SCENE_MEMORY_PROMPT = """\
Summarize one scene from a Ukrainian Python programming course.

Scene {scene_id}: {n} frames, {ts_start:.0f}s to {ts_end:.0f}s.

Frame descriptions:
{descriptions}

Complete extracted text/code:
{merged_text}

Respond as JSON:
{{
  "scene_type": "<dominant type>",
  "summary": "<Ukrainian, 2-3 sentences>",
  "complete_text": "<cleaned merged text>",
  "topics": ["topic1", "topic2"],
  "importance": <1-5>
}}
"""

COURSE_MEMORY_PROMPT = """\
Update the running summary of a Ukrainian Python programming course video.

Previous context:
{previous_context}

New scene just processed:
{scene_summary}
Topics: {topics}

Update the running context (max 200 words, Ukrainian). \
Focus on: topics covered, code examples shown, current teaching point.

Respond with the updated context only (plain text, no JSON).
"""


def _build_eyes_parts(
    frame: dict,
    prev_frames: list[dict],
    all_frames: list[dict],
    course_ctx: str,
    scene_ctx: list[dict],
    genai_mod,
) -> list:
    """Build Gemini API parts: [prev_images..., MAIN_image, prompt_text]."""
    parts = []

    # Previous images (within CONTEXT_IMG_MAX_GAP seconds, same scene)
    for pf in prev_frames:
        gap = frame["timestamp"] - pf["timestamp"]
        if 0 < gap <= CONTEXT_IMG_MAX_GAP:
            img_path = _frame_dir(pf) / pf["filename"]
            parts.append(
                genai_mod.types.Part.from_bytes(
                    data=img_path.read_bytes(),
                    mime_type="image/jpeg",
                )
            )

    # MAIN image (current frame)
    main_path = _frame_dir(frame) / frame["filename"]
    parts.append(
        genai_mod.types.Part.from_bytes(
            data=main_path.read_bytes(),
            mime_type="image/jpeg",
        )
    )

    # Build prompt text
    course_block = ""
    if course_ctx:
        course_block = f"Course context (what has been covered):\n{course_ctx}\n\n"

    scene_block = ""
    if scene_ctx:
        items = "\n".join(
            f"- {item['ts']:.0f}s: {item['desc']}" for item in scene_ctx[-5:]
        )
        scene_block = f"Previous frames in this scene:\n{items}\n\n"

    prompt = EYES_PROMPT.format(
        course_context_block=course_block,
        scene_context_block=scene_block,
    )
    parts.append(genai_mod.types.Part.from_text(text=prompt))

    return parts


def _merge_code_texts(base: str, new: str) -> str:
    """Merge two code text extractions by finding overlap."""
    base_lines = base.strip().splitlines()
    new_lines = new.strip().splitlines()
    if not new_lines:
        return base
    if not base_lines:
        return new

    best_overlap = 0
    for overlap_len in range(1, min(len(base_lines), len(new_lines)) + 1):
        if all(
            b.strip() == n.strip()
            for b, n in zip(
                base_lines[-overlap_len:],
                new_lines[:overlap_len],
                strict=False,
            )
        ):
            best_overlap = overlap_len

    if best_overlap > 0:
        return "\n".join(base_lines + new_lines[best_overlap:])
    return base + "\n\n--- next frame ---\n\n" + new


def _normalize_response(text: str) -> str:
    """Extract text content from response for accuracy comparison.

    Handles both old JSON format (extracted_text field) and new Markdown
    format (## Element N sections with code blocks).
    """
    stripped = text.strip()

    # Try old JSON format first (backwards compat)
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

    # New Markdown format: collect all text from ## Element sections
    element_lines: list[str] = []
    in_element = False
    for line in text.splitlines():
        if line.startswith("## Element"):
            in_element = True
            continue
        if line.startswith("## ") and in_element:
            # Next top-level section (e.g. ## Scene Composition again) — stop
            if "Element" not in line:
                in_element = False
                continue
            # Another ## Element — continue
            continue
        if in_element:
            element_lines.append(line)

    if element_lines:
        return "\n".join(element_lines)

    return text


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


def step_pipeline(
    frames_state: dict,
    *,
    vision_model: str = VISION_MODEL,
    text_model: str = TEXT_MODEL,
    scene_filter: set[int] | None = None,
    dry_run: bool = False,
) -> None:
    """Run Eyes + Memory pipeline scene by scene.

    State files namespaced by vision_model.
    """
    import google.genai as genai

    keys = _get_gemini_keys()
    if not keys and not dry_run:
        log.error("No GEMINI_API_KEY in .env")
        return

    # RPM lookup
    model_rpms = {
        "gemini-2.5-flash": 5,
        "gemini-3.1-flash-lite-preview": 15,
        "gemini-2.5-flash-lite": 10,
        "gemini-3-flash-preview": 5,
    }
    vision_rpm = model_rpms.get(vision_model, 5)
    text_rpm = model_rpms.get(text_model, 15)

    vision_pool = GeminiPool(keys, rpm=vision_rpm) if not dry_run else None
    text_pool = GeminiPool(keys, rpm=text_rpm) if not dry_run else None

    log.info(
        "Vision model: %s (%d RPM), Text model: %s (%d RPM)",
        vision_model,
        vision_rpm,
        text_model,
        text_rpm,
    )

    frames = frames_state["frames"]
    scenes = frames_state["scenes"]

    # Load existing state — namespaced by vision_model
    mid = vision_model
    eyes_state = load_model_state("eyes", mid) or {"model": mid, "results": {}}
    instant_state = load_model_state("instant_memory", mid) or {
        "model": mid,
        "results": {},
    }
    scene_mem_state = load_model_state("scene_memory", mid) or {
        "model": mid,
        "results": {},
    }
    course_mem_state = load_model_state("course_memory", mid) or {
        "model": mid,
        "context": "",
        "scenes_processed": -1,
        "history": [],
    }

    course_ctx = course_mem_state["context"]

    for scene in scenes:
        sid = scene["id"]
        if scene_filter is not None and sid not in scene_filter:
            continue

        # Skip fully processed scenes
        if (
            str(sid) in instant_state["results"]
            and str(sid) in scene_mem_state["results"]
            and course_mem_state["scenes_processed"] >= sid
        ):
            course_ctx = course_mem_state["context"]
            log.info("Scene %d: fully cached, skip", sid)
            continue

        scene_frames = frames[scene["start"] : scene["end"] + 1]
        log.info(
            "Scene %d: %d frames, ts %.0f–%.0fs, course_ctx=%d words",
            sid,
            len(scene_frames),
            scene["ts_range"][0],
            scene["ts_range"][1],
            len(course_ctx.split()),
        )

        # ── Eyes ──────────────────────────────────────────────
        scene_ctx_items: list[dict] = []

        for i, frame in enumerate(scene_frames):
            fid = frame["frame_id"]

            if fid in eyes_state["results"]:
                # Already done — restore scene context
                r = eyes_state["results"][fid]
                if "description" in r:
                    scene_ctx_items.append(
                        {
                            "ts": frame["timestamp"],
                            "desc": r["description"],
                        }
                    )
                log.info("  [eyes] %s: cached", fid)
                continue

            if dry_run:
                log.info("  [eyes] %s: would call Vision LLM (dry run)", fid)
                continue

            # Build previous frames for context images (within 7s)
            prev_for_img = []
            for j in range(max(0, i - 2), i):
                pf = scene_frames[j]
                if frame["timestamp"] - pf["timestamp"] <= CONTEXT_IMG_MAX_GAP:
                    prev_for_img.append(pf)

            parts = _build_eyes_parts(
                frame,
                prev_for_img,
                frames,
                course_ctx,
                scene_ctx_items,
                genai,
            )
            n_images = len(prev_for_img) + 1

            try:
                result = vision_pool.call(vision_model, parts)  # type: ignore[union-attr]
                eyes_state["results"][fid] = {
                    "frame_id": fid,
                    "timestamp": frame["timestamp"],
                    "scene_id": sid,
                    "response": result["text"],
                    "n_images": n_images,
                    "latency_sec": result["latency_sec"],
                    "input_tokens": result["input_tokens"],
                    "output_tokens": result["output_tokens"],
                    "timestamp_saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }

                # Parse description for scene context
                resp_text = result["text"]
                desc = ""
                scene_type = ""
                importance = 3

                # Try JSON format (old)
                try:
                    inner = resp_text.strip()
                    if inner.startswith("```"):
                        lines = inner.splitlines()[1:]
                        if lines and lines[-1].strip() == "```":
                            lines = lines[:-1]
                        inner = "\n".join(lines)
                    parsed = json.loads(inner)
                    desc = parsed.get("description", "")
                    scene_type = parsed.get("scene_type", "")
                    importance = parsed.get("importance", 3)
                except (json.JSONDecodeError, ValueError):
                    pass

                # Try Markdown format (new): extract from Scene Composition
                if not desc:
                    for line in resp_text.splitlines():
                        if line.startswith("**Setting:**"):
                            scene_type = line.split("**Setting:**")[-1].strip()
                        if line.startswith("**Elements:**") or line.startswith(
                            "**People:**"
                        ):
                            # Use first few element lines as description
                            break
                    # Use first 2 lines after ## Scene Composition as desc
                    in_comp = False
                    desc_parts: list[str] = []
                    for line in resp_text.splitlines():
                        if "## Scene Composition" in line:
                            in_comp = True
                            continue
                        if in_comp and line.startswith("## "):
                            break
                        if in_comp and line.strip():
                            desc_parts.append(line.strip())
                    desc = " ".join(desc_parts[:3]) if desc_parts else resp_text[:200]

                eyes_state["results"][fid]["description"] = desc[:300]
                eyes_state["results"][fid]["scene_type"] = scene_type
                eyes_state["results"][fid]["importance"] = importance

                scene_ctx_items.append({"ts": frame["timestamp"], "desc": desc})
                save_model_state("eyes", mid, eyes_state)
                log.info(
                    "  [eyes] %s OK (%d img, %.1fs, %d+%d tok)",
                    fid,
                    n_images,
                    result["latency_sec"],
                    result["input_tokens"],
                    result["output_tokens"],
                )

            except Exception as e:
                log.error("  [eyes] %s FAILED: %s", fid, str(e)[:200])
                save_model_state("eyes", mid, eyes_state)
                return  # Stop — can resume later

        if dry_run:
            continue

        # ── Instant Memory ────────────────────────────────────
        if str(sid) not in instant_state["results"]:
            texts = []
            for frame in scene_frames:
                r = eyes_state["results"].get(frame["frame_id"], {})
                raw = r.get("response", "")
                texts.append(_normalize_response(raw))

            if len(texts) == 1:
                merged = texts[0]
                method = "single"
            else:
                merged = texts[0]
                for t in texts[1:]:
                    merged = _merge_code_texts(merged, t)

                # If merge has too many "next frame" separators → LLM fallback
                if merged.count("--- next frame ---") > len(texts) // 2:
                    frame_texts = "\n\n".join(
                        f"Frame {j + 1} ({scene_frames[j]['timestamp']:.0f}s):\n{t}"
                        for j, t in enumerate(texts)
                    )
                    prompt = INSTANT_MERGE_PROMPT.format(
                        n=len(texts),
                        frame_texts=frame_texts,
                    )
                    try:
                        parts = [genai.types.Part.from_text(text=prompt)]
                        result = text_pool.call(text_model, parts)  # type: ignore[union-attr]
                        merged = result["text"]
                        method = "llm_merge"
                        log.info(
                            "  [instant] Scene %d: LLM merge (%d frames)",
                            sid,
                            len(texts),
                        )
                    except Exception as e:
                        log.warning("  [instant] LLM merge failed: %s", str(e)[:100])
                        method = "code_diff_fallback"
                else:
                    method = "code_diff"

            instant_state["results"][str(sid)] = {
                "scene_id": sid,
                "merged_text": merged,
                "frame_count": len(texts),
                "method": method,
            }
            save_model_state("instant_memory", mid, instant_state)
            log.info("  [instant] Scene %d: %s (%d frames)", sid, method, len(texts))

        # ── Scene Memory ──────────────────────────────────────
        if str(sid) not in scene_mem_state["results"]:
            eyes_results = eyes_state["results"]
            descriptions = "\n".join(
                f"- {f['timestamp']:.0f}s: "
                f"{eyes_results.get(f['frame_id'], {}).get('description', '?')}"
                for f in scene_frames
            )
            merged_text = instant_state["results"][str(sid)]["merged_text"]

            prompt = SCENE_MEMORY_PROMPT.format(
                scene_id=sid,
                n=len(scene_frames),
                ts_start=scene["ts_range"][0],
                ts_end=scene["ts_range"][1],
                descriptions=descriptions,
                merged_text=merged_text[:2000],  # cap for token safety
            )
            try:
                parts = [genai.types.Part.from_text(text=prompt)]
                result = text_pool.call(text_model, parts)  # type: ignore[union-attr]
                scene_mem_state["results"][str(sid)] = {
                    "scene_id": sid,
                    "response": result["text"],
                    "latency_sec": result["latency_sec"],
                }
                save_model_state("scene_memory", mid, scene_mem_state)
                log.info(
                    "  [scene] Scene %d: synthesized (%.1fs)",
                    sid,
                    result["latency_sec"],
                )
            except Exception as e:
                log.error("  [scene] Scene %d FAILED: %s", sid, str(e)[:200])
                save_model_state("scene_memory", mid, scene_mem_state)
                return

        # ── Course Memory ─────────────────────────────────────
        if course_mem_state["scenes_processed"] < sid:
            scene_resp = scene_mem_state["results"][str(sid)].get("response", "")

            # Parse scene summary
            summary = scene_resp[:200]
            topics = ""
            try:
                inner = scene_resp.strip()
                if inner.startswith("```"):
                    lines = inner.splitlines()[1:]
                    if lines and lines[-1].strip() == "```":
                        lines = lines[:-1]
                    inner = "\n".join(lines)
                parsed = json.loads(inner)
                summary = parsed.get("summary", summary)
                topics = ", ".join(parsed.get("topics", []))
            except (json.JSONDecodeError, ValueError):
                pass

            # Skip LLM for low-importance scenes
            importance = (
                eyes_state["results"]
                .get(
                    scene_frames[0]["frame_id"],
                    {},
                )
                .get("importance", 3)
            )

            if importance <= 2 and course_ctx:
                course_ctx += f"\n+ scene {sid}: {summary[:80]}"
                log.info("  [course] Scene %d: appended (low importance)", sid)
            else:
                prompt = COURSE_MEMORY_PROMPT.format(
                    previous_context=course_ctx or "(beginning of video)",
                    scene_summary=summary,
                    topics=topics or "N/A",
                )
                try:
                    parts = [genai.types.Part.from_text(text=prompt)]
                    result = text_pool.call(text_model, parts)  # type: ignore[union-attr]
                    course_ctx = result["text"]
                    log.info(
                        "  [course] Scene %d: updated (%.1fs)",
                        sid,
                        result["latency_sec"],
                    )
                except Exception as e:
                    log.warning("  [course] Scene %d failed: %s", sid, str(e)[:100])

            course_mem_state["context"] = course_ctx
            course_mem_state["scenes_processed"] = sid
            course_mem_state["history"].append(
                {
                    "scene_id": sid,
                    "context_snapshot": course_ctx[:500],
                }
            )
            save_model_state("course_memory", mid, course_mem_state)

    log.info("Pipeline complete for %d scenes", len(scenes))


# ═══════════════════════════════════════════════════════════════════
# STEP: eval — accuracy against ground truth (NO API)
# ═══════════════════════════════════════════════════════════════════


def _char_accuracy(extracted: str, ground_truth: str) -> float:
    """Greedy sequential character matching."""
    gt = ground_truth.strip()
    ex = extracted.strip()
    if not gt:
        return 1.0 if not ex else 0.0
    matches = gi = ei = 0
    while gi < len(gt) and ei < len(ex):
        if gt[gi] == ex[ei]:
            matches += 1
            gi += 1
            ei += 1
        else:
            ei += 1
    return matches / len(gt)


def step_eval(frames_state: dict, vision_model: str = VISION_MODEL) -> None:
    """Evaluate accuracy against ground truth for a specific model."""
    mid = vision_model
    eyes_state = load_model_state("eyes", mid) or {"results": {}}
    instant_state = load_model_state("instant_memory", mid) or {"results": {}}
    log.info("Evaluating model: %s (%d eyes results)", mid, len(eyes_state["results"]))
    frames = frames_state["frames"]

    # Load v1 baseline for comparison
    v1_path = Path("current-doc/vd-spike/VD-SPIKE-B/results.json")
    v1_accs: dict[str, float] = {}
    if v1_path.exists():
        v1 = json.loads(v1_path.read_text())
        for key in ["gemini_flash_ocr", "gemini_flash_combined", "gpt4o_combined"]:
            entry = v1.get(key, {})
            for a in entry.get("accuracies", []):
                v1_accs.setdefault(a["frame"], {})[key] = a["accuracy"]

    evals: list[dict] = []

    for f in frames:
        if not f.get("has_gt"):
            continue

        gt_path = GT_DIR / f["filename"].replace(".jpg", ".txt")
        if not gt_path.exists():
            continue
        gt_text = _extract_code_blocks(gt_path.read_text())
        if not gt_text.strip():
            continue

        fid = f["frame_id"]
        eyes_r = eyes_state["results"].get(fid, {})
        raw_response = eyes_r.get("response", "")
        eyes_normalized = _normalize_response(raw_response)
        eyes_code = _extract_code_blocks(eyes_normalized)

        # Find scene for this frame
        scene_id = eyes_r.get("scene_id")
        merged_text = ""
        if scene_id is not None:
            instant = instant_state["results"].get(str(scene_id), {})
            merged_text = instant.get("merged_text", "")
        merged_code = _extract_code_blocks(merged_text) if merged_text else ""

        eyes_acc = round(_char_accuracy(eyes_code, gt_text) * 100, 1)
        merged_acc = (
            round(_char_accuracy(merged_code, gt_text) * 100, 1) if merged_code else 0
        )

        # v1 baselines
        v1_best = 0
        v1_info = v1_accs.get(f["filename"], {})
        if v1_info:
            v1_best = max(v1_info.values())

        evals.append(
            {
                "frame": f["filename"],
                "eyes_accuracy": eyes_acc,
                "merged_accuracy": merged_acc,
                "v1_best": v1_best,
                "delta_eyes": round(eyes_acc - v1_best, 1),
                "delta_merged": round(merged_acc - v1_best, 1),
            }
        )

    if not evals:
        log.info("No GT frames with results to evaluate")
        return

    avg_eyes = round(sum(e["eyes_accuracy"] for e in evals) / len(evals), 1)
    avg_merged = round(sum(e["merged_accuracy"] for e in evals) / len(evals), 1)
    avg_v1 = round(sum(e["v1_best"] for e in evals) / len(evals), 1)

    save_model_state(
        "eval",
        mid,
        {
            "model": mid,
            "evals": evals,
            "avg_eyes": avg_eyes,
            "avg_merged": avg_merged,
            "avg_v1_best": avg_v1,
        },
    )

    log.info("=" * 70)
    log.info("EVALUATION (vs v1 baseline)")
    log.info("=" * 70)
    log.info(
        "%-25s %8s %8s %8s %8s %8s",
        "Frame",
        "v1_best",
        "Eyes",
        "Merged",
        "Δ Eyes",
        "Δ Merged",
    )
    for e in evals:
        log.info(
            "%-25s %7.1f%% %7.1f%% %7.1f%% %+7.1f%% %+7.1f%%",
            e["frame"],
            e["v1_best"],
            e["eyes_accuracy"],
            e["merged_accuracy"],
            e["delta_eyes"],
            e["delta_merged"],
        )
    log.info("-" * 70)
    log.info(
        "%-25s %7.1f%% %7.1f%% %7.1f%% %+7.1f%% %+7.1f%%",
        "AVERAGE",
        avg_v1,
        avg_eyes,
        avg_merged,
        round(avg_eyes - avg_v1, 1),
        round(avg_merged - avg_v1, 1),
    )
    log.info("=" * 70)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════


def step_summary(frames_state: dict) -> None:
    """Compare all tested models."""
    log.info("=" * 70)
    log.info("MULTI-MODEL SUMMARY")
    log.info("=" * 70)

    models_to_check = [
        "gemini-2.5-flash",
        "gemini-3.1-flash-lite-preview",
        "gemini-2.5-flash-lite",
        "gemini-3-flash-preview",
    ]

    for mid in models_to_check:
        eyes = load_model_state("eyes", mid)
        ev = load_model_state("eval", mid)
        if not eyes:
            continue
        n_done = len(eyes.get("results", {}))
        avg = ev.get("avg_eyes", "?") if ev else "not evaluated"
        merged = ev.get("avg_merged", "?") if ev else "?"
        log.info(
            "  %-35s %d/151 frames  eyes=%s%%  merged=%s%%", mid, n_done, avg, merged
        )

    log.info("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="VD Pipeline v3 multi-model")
    parser.add_argument(
        "--step",
        choices=["frames", "pipeline", "eval", "summary", "all"],
        default="all",
    )
    parser.add_argument(
        "--model",
        default="gemini-3.1-flash-lite-preview",
        help="Model for ALL calls (vision + text)",
    )
    parser.add_argument(
        "--text-model",
        default=None,
        help="Override text model (default: same as --model)",
    )
    parser.add_argument("--scene", type=int, default=None, help="Process single scene")
    parser.add_argument(
        "--scenes", help="Comma-separated scene IDs (e.g. '6,7,8,14,15')"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    step = args.step

    if step in ("all", "frames"):
        frames_state = step_frames()
    else:
        frames_state = load_state("frames")
        if not frames_state:
            log.error("Run --step frames first (or use spike_vd_pipeline.py)")
            return

    text_model = args.text_model or args.model  # default: same as vision

    # Parse scene filter
    scene_filter: set[int] | None = None
    if args.scene is not None:
        scene_filter = {args.scene}
    elif args.scenes:
        scene_filter = {int(s) for s in args.scenes.split(",")}

    if step in ("all", "pipeline"):
        step_pipeline(
            frames_state,
            vision_model=args.model,
            text_model=text_model,
            scene_filter=scene_filter,
            dry_run=args.dry_run,
        )

    if step in ("all", "eval"):
        step_eval(frames_state, vision_model=args.model)

    if step == "summary":
        step_summary(frames_state)


if __name__ == "__main__":
    main()
