"""Trace a single frame: show prompt, response, GT, accuracy."""

import json
import sys
from pathlib import Path

BASE = Path("current-doc/vd-spike/VD-SPIKE-B/pipeline")
GT_DIR = Path("current-doc/vd-spike/VD-SPIKE-B/ground-truth")


def trace(frame_name: str, model_slug: str = "gemini_3_1_flash_lite_preview") -> None:
    eyes = json.loads((BASE / f"eyes_{model_slug}.json").read_text())
    frames_state = json.loads((BASE / "frames.json").read_text())
    frames = frames_state["frames"]
    scenes = frames_state["scenes"]
    course_mem = json.loads((BASE / f"course_memory_{model_slug}.json").read_text())

    fid = frame_name.replace(".jpg", "")
    r = eyes["results"].get(fid)
    if not r:
        print(f"No result for {fid}")
        return

    sid = r["scene_id"]
    scene = scenes[sid]
    scene_fids = [
        frames[i]["frame_id"] for i in range(scene["start"], scene["end"] + 1)
    ]
    idx = scene_fids.index(fid)

    # Course context
    prev_ctx = ""
    for h in course_mem.get("history", []):
        if h["scene_id"] < sid:
            prev_ctx = h.get("context_snapshot", "")

    print(f"Frame: {fid}")
    print(f"Scene: {sid}, position: {idx}/{len(scene_fids)}, n_images: {r['n_images']}")
    print()

    print("=" * 60)
    print("COURSE CONTEXT (fed to prompt)")
    print("=" * 60)
    print(prev_ctx[:500] if prev_ctx else "(empty)")
    print()

    print("=" * 60)
    print("SCENE CONTEXT (fed to prompt)")
    print("=" * 60)
    if idx == 0:
        print("(none — first frame in scene)")
    else:
        for prev_fid in scene_fids[:idx]:
            prev_r = eyes["results"].get(prev_fid, {})
            desc = prev_r.get("description", "?")
            print(f"  {prev_fid}: {desc[:120]}")
    print()

    print("=" * 60)
    print("PROMPT TEMPLATE")
    print("=" * 60)
    course_block = (
        f"Course context (what has been covered):\n{prev_ctx}\n\n" if prev_ctx else ""
    )
    scene_block = ""
    if idx > 0:
        items = []
        for prev_fid in scene_fids[:idx]:
            prev_r = eyes["results"].get(prev_fid, {})
            ts = prev_r.get("timestamp", 0)
            desc = prev_r.get("description", "")
            items.append(f"- {ts:.0f}s: {desc}")
        scene_block = (
            "Previous frames in this scene:\n" + "\n".join(items[-5:]) + "\n\n"
        )

    print("[IMAGES: " + ", ".join(["prev"] * (r["n_images"] - 1) + ["MAIN"]) + "]")
    print()
    print(
        "You are analyzing frames from a Ukrainian Python programming course video.\n"
        "The LAST image is the MAIN frame to describe...\n"
    )
    print(course_block + scene_block + "Analyze the MAIN (last) frame...\n")
    print(
        "Rules for extracted_text:\n"
        "- Python code: preserve EXACT indentation, wrap in ```python``` blocks\n"
        "- Console: include >>> prompts AND output\n"
        "- Slides: extract headings and bullet points\n"
        "- Multiple code areas: extract EACH separately with labels\n"
        "- Extract ONLY what is visible."
    )
    print()

    print("=" * 60)
    print("MODEL RESPONSE (raw)")
    print("=" * 60)
    print(r["response"][:1500])
    print()

    # Parse extracted content from response
    extracted = ""
    resp = r["response"]

    # Try JSON format (old)
    try:
        inner = resp.strip()
        if inner.startswith("```"):
            lines = inner.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            inner = "\n".join(lines)
        parsed = json.loads(inner)
        extracted = parsed.get("extracted_text", "")
    except (json.JSONDecodeError, ValueError):
        pass

    # Try Markdown format (new): collect from ## Element sections
    if not extracted:
        element_lines = []
        in_element = False
        for line in resp.splitlines():
            if line.startswith("## Element"):
                in_element = True
                continue
            if line.startswith("## ") and in_element and "Element" not in line:
                in_element = False
                continue
            if in_element:
                element_lines.append(line)
        if element_lines:
            extracted = "\n".join(element_lines)

    print("=" * 60)
    print("EXTRACTED TEXT (parsed from response)")
    print("=" * 60)
    print(extracted[:800])
    print()

    # Ground truth
    gt_path = GT_DIR / (fid + ".txt")
    if gt_path.exists():
        gt = gt_path.read_text()
        print("=" * 60)
        print("GROUND TRUTH")
        print("=" * 60)
        print(gt[:800])
        print()

        # Accuracy
        def extract_blocks(text):
            blocks, in_b, cur = [], False, []
            for line in text.splitlines():
                if line.strip().startswith("```"):
                    if in_b:
                        blocks.append("\n".join(cur))
                        cur = []
                        in_b = False
                    else:
                        in_b = True
                elif in_b:
                    cur.append(line)
            if cur:
                blocks.append("\n".join(cur))
            return "\n---\n".join(blocks)

        gt_code = extract_blocks(gt).strip()
        ex_code = extract_blocks(extracted).strip() if extracted else extracted.strip()

        # If no code blocks in extracted, use raw text
        if not ex_code:
            ex_code = extracted.strip()

        print("=" * 60)
        print("ACCURACY COMPARISON")
        print("=" * 60)
        print(f"GT code ({len(gt_code)} chars):")
        print(gt_code[:400])
        print()
        print(f"Extracted code ({len(ex_code)} chars):")
        print(ex_code[:400])
        print()

        if gt_code:
            matches = gi = ei = 0
            while gi < len(gt_code) and ei < len(ex_code):
                if gt_code[gi] == ex_code[ei]:
                    matches += 1
                    gi += 1
                    ei += 1
                else:
                    ei += 1
            acc = matches / len(gt_code) * 100
            print(f"Char accuracy: {acc:.1f}% ({matches}/{len(gt_code)} chars matched)")
        else:
            print("No GT code to compare")


if __name__ == "__main__":
    frame = sys.argv[1] if len(sys.argv) > 1 else "golden_012_210s.jpg"
    model = sys.argv[2] if len(sys.argv) > 2 else "gemini_3_1_flash_lite_preview"
    trace(frame, model)
