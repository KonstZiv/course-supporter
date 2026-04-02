"""Test new prompt format: JSON metadata + Markdown extracted text."""

import os
import time
from pathlib import Path

import google.genai as genai

# Load env
for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    if "=" in line:
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

keys = list(
    dict.fromkeys(
        k.strip() for k in os.environ.get("GEMINI_API_KEY", "").split(",") if k.strip()
    )
)

GOLDEN_DIR = Path("current-doc/vd-spike/golden-frames-sample2")

NEW_PROMPT = """\
You are analyzing a video frame. Describe EVERYTHING visible as precisely as \
possible — this description is the raw data for further processing.

Respond in Markdown with these sections:

## Scene Composition

**Layout:** Describe the overall scene (e.g. screen recording of an IDE, \
lecture hall with a whiteboard, presentation with speaker, etc.)

**Elements:** Numbered list of ALL distinct visual areas/objects and their \
positions on screen. For each: what it is, where it is located.

## Element 1: <name> (<position>)

Describe the content of this element in full detail:
- Text: reproduce EXACTLY as visible, preserving formatting
- Code: wrap in fenced code blocks with language tag if recognizable \
(```python```, ```bash```, ```js```, etc.), or plain ``` if unknown
- Images/diagrams: describe what is depicted
- Handwriting/whiteboard: transcribe text, describe drawings

## Element 2: <name> (<position>)

(same structure for each element)

---

Rules:
- Extract ONLY what is visible. Do NOT infer, complete, or hallucinate content.
- Preserve exact text: indentation, punctuation, special characters.
- If text is partially obscured, extract what IS visible and note "[partially hidden]".
- Use the language of the original content (do not translate).
- Describe people if present: position, what they point at, where they look.
{context_block}"""


def test_frame(frame_name: str, model_id: str = "gemini-2.5-flash") -> None:
    """Send one frame with new prompt, print full response."""
    img_path = GOLDEN_DIR / frame_name
    if not img_path.exists():
        print(f"Frame not found: {img_path}")
        return

    client = genai.Client(api_key=keys[0])
    img_data = img_path.read_bytes()

    # No context for clean test
    prompt = NEW_PROMPT.format(context_block="")

    print(f"=== Testing {frame_name} with {model_id} ===")
    print(f"Prompt ({len(prompt)} chars):")
    print(prompt)
    print()

    parts = [
        genai.types.Part.from_bytes(data=img_data, mime_type="image/jpeg"),
        genai.types.Part.from_text(text=prompt),
    ]

    t0 = time.time()
    response = client.models.generate_content(
        model=model_id,
        contents=[genai.types.Content(parts=parts)],
    )
    latency = time.time() - t0

    text = response.text or ""
    usage = response.usage_metadata
    in_tok = usage.prompt_token_count if usage else 0
    out_tok = usage.candidates_token_count if usage else 0

    print(f"=== RESPONSE ({latency:.1f}s, {in_tok}+{out_tok} tok) ===")
    print(text)
    print()

    # Parse: extract code blocks from "## Extracted Text" section
    in_extracted = False
    extracted_lines = []
    for line in text.splitlines():
        if line.strip().startswith("## Extracted Text"):
            in_extracted = True
            continue
        if in_extracted and line.strip().startswith("## ") and "Extracted" not in line:
            break
        if in_extracted:
            extracted_lines.append(line)

    extracted_md = "\n".join(extracted_lines).strip()

    # Extract code blocks
    blocks = []
    in_block = False
    current = []
    for line in extracted_md.splitlines():
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

    code_text = "\n---\n".join(blocks)

    print("=== PARSED: Extracted Text (MD) ===")
    print(extracted_md[:600])
    print()
    print("=== PARSED: Code blocks only ===")
    print(code_text[:400])
    print()

    # Compare with GT if exists
    gt_path = Path("current-doc/vd-spike/VD-SPIKE-B/ground-truth") / frame_name.replace(
        ".jpg", ".txt"
    )
    if gt_path.exists():
        gt = gt_path.read_text()
        gt_blocks = []
        in_b = False
        cur = []
        for line in gt.splitlines():
            if line.strip().startswith("```"):
                if in_b:
                    gt_blocks.append("\n".join(cur))
                    cur = []
                    in_b = False
                else:
                    in_b = True
            elif in_b:
                cur.append(line)
        if cur:
            gt_blocks.append("\n".join(cur))
        gt_code = "\n---\n".join(gt_blocks).strip()

        print("=== GROUND TRUTH (code only) ===")
        print(gt_code[:400])
        print()

        if gt_code:
            matches = gi = ei = 0
            while gi < len(gt_code) and ei < len(code_text):
                if gt_code[gi] == code_text[ei]:
                    matches += 1
                    gi += 1
                    ei += 1
                else:
                    ei += 1
            acc = matches / len(gt_code) * 100
            print(f"=== ACCURACY: {acc:.1f}% ({matches}/{len(gt_code)}) ===")


if __name__ == "__main__":
    import sys

    frame = sys.argv[1] if len(sys.argv) > 1 else "golden_012_210s.jpg"
    model = sys.argv[2] if len(sys.argv) > 2 else "gemini-2.5-flash"
    test_frame(frame, model)
