"""Generate HTML report for VD-017 outline spike per video."""

from __future__ import annotations

import html as html_mod
import json
import sys
from pathlib import Path

OUT_DIR = Path("tmp/vd-017-spike")

MODELS = [
    "claude-sonnet-4-20250514",
    "gpt-4o",
    "gpt-4.1",
    "deepseek-chat",
]

CSS = """
body { font-family: system-ui, sans-serif; margin: 0; padding: 20px;
       background: #0a0a1a; color: #eee; max-width: 1800px; margin: 0 auto; line-height: 1.5; }
h1 { color: #e94560; margin-bottom: 4px; }
h2 { color: #4ecca3; border-bottom: 2px solid #4ecca3; padding: 8px 0; margin-top: 40px; }
h3 { color: #e9a945; margin-top: 24px; }
h4 { color: #7ec8e3; margin-top: 16px; margin-bottom: 8px; }
.subtitle { color: #888; margin-top: 0; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
         gap: 10px; margin: 16px 0; }
.stat-card { background: #16213e; border-radius: 8px; padding: 12px; text-align: center; }
.stat-value { font-size: 24px; font-weight: bold; color: #4ecca3; }
.stat-label { font-size: 11px; color: #888; margin-top: 4px; }
.model-block { background: #16213e; border-radius: 8px; padding: 16px; margin: 12px 0; }
.section-block { background: #1a1a2e; border-radius: 6px; padding: 12px; margin: 8px 0;
                 border-left: 3px solid #4ecca3; }
.code-block { background: #0d1117; border-radius: 4px; padding: 10px; margin: 6px 0;
              font-family: 'SF Mono', Monaco, monospace; font-size: 13px; white-space: pre-wrap;
              overflow-x: auto; color: #c9d1d9; }
.narration { background: #1e2d3d; border-radius: 4px; padding: 10px; margin: 6px 0;
             line-height: 1.6; white-space: pre-wrap; }
.screen { background: #1d2d1d; border-radius: 4px; padding: 10px; margin: 6px 0;
          line-height: 1.5; white-space: pre-wrap; color: #a8d8a8; }
.concepts { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
.concept-tag { background: #3a7bd5; color: #fff; padding: 2px 8px; border-radius: 4px;
               font-size: 12px; }
.raw-block { background: #1a1a1a; border-radius: 6px; padding: 12px; margin: 8px 0;
             border-left: 3px solid #e94560; }
.raw-text { font-size: 13px; color: #ccc; white-space: pre-wrap; line-height: 1.5; }
.time-badge { background: #333; color: #4ecca3; padding: 1px 6px; border-radius: 3px;
              font-size: 11px; font-family: monospace; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }
th { background: #16213e; color: #4ecca3; }
td { vertical-align: top; }
.compare-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 900px) { .compare-grid { grid-template-columns: 1fr; } }
"""


def esc(t: object) -> str:
    return html_mod.escape(str(t))


def generate_report(vid_slug: str, video_title: str, report_name: str) -> None:
    stt = json.loads((OUT_DIR / f"stt_{vid_slug}.json").read_text())
    vd = json.loads((OUT_DIR / f"vd_{vid_slug}.json").read_text())

    outlines: dict[str, dict] = {}
    for m in MODELS:
        p = OUT_DIR / f"outline_{vid_slug}_{m}.json"
        if p.exists():
            outlines[m] = json.loads(p.read_text())

    duration = max((s["end_sec"] for s in stt["segments"]), default=0)
    vd_stats = vd.get("stats", {})

    parts: list[str] = []
    parts.append(
        f"<html><head><meta charset='utf-8'>"
        f"<title>VD-017 {esc(video_title)}</title>"
        f"<style>{CSS}</style></head><body>"
    )
    parts.append(f"<h1>VD-017: Material Outline — {esc(video_title)}</h1>")
    parts.append(f'<p class="subtitle">{esc(video_title)} — {duration:.0f}s</p>')

    # === 1. RAW INPUT ===
    parts.append("<h2>1. Raw Input (SourceDocument)</h2>")
    parts.append('<div class="stats">')
    for label, val in [
        ("STT segments", len(stt["segments"])),
        ("VD scenes", vd_stats.get("processed_scenes", "?")),
        ("VD frames", vd_stats.get("total_frames", "?")),
        ("STT provider", stt.get("provider", "?")),
        ("Duration", f"{duration:.0f}s"),
    ]:
        parts.append(
            f'<div class="stat-card"><div class="stat-value">{val}</div>'
            f'<div class="stat-label">{label}</div></div>'
        )
    parts.append("</div>")

    parts.append("<h3>STT Transcript (raw)</h3>")
    for seg in stt["segments"]:
        parts.append(
            f'<div class="raw-block">'
            f'<span class="time-badge">{seg["start_sec"]:.1f}s – {seg["end_sec"]:.1f}s</span>'
            f'<div class="raw-text">{esc(seg["text"])}</div></div>'
        )

    parts.append("<h3>VD Scenes (raw)</h3>")
    for scene in vd["scenes"]:
        sm = scene["scene_memory"]
        parts.append(
            f'<div class="raw-block">'
            f'<span class="time-badge">Scene {scene["scene_id"]}: '
            f"{scene['start_sec']:.0f}–{scene['end_sec']:.0f}s</span> "
            f"({scene['n_frames']} frames, type={sm['scene_type']})"
            f'<div class="raw-text">{esc(sm["summary"])}</div></div>'
        )

    # === 2. MODEL COMPARISON ===
    parts.append("<h2>2. Model Comparison</h2>")
    parts.append(
        "<table><tr><th>Model</th><th>Sections</th><th>Tokens In</th>"
        "<th>Tokens Out</th><th>Cost</th><th>Time</th><th>Provider</th></tr>"
    )
    for m in MODELS:
        if m not in outlines:
            continue
        o = outlines[m]
        ol = o["outline"]
        parts.append(
            f"<tr><td><b>{esc(o['model_label'])}</b></td>"
            f"<td>{len(ol.get('sections', []))}</td>"
            f"<td>{o['tokens_in']:,}</td>"
            f"<td>{o['tokens_out']:,}</td>"
            f"<td>${o['cost_usd']:.4f}</td>"
            f"<td>{o['elapsed_sec']:.1f}s</td>"
            f"<td>{o['provider']}</td></tr>"
        )
    parts.append("</table>")

    # === 3. MACRO FIELDS ===
    parts.append("<h2>3. Navigation Layer (Macro Fields)</h2>")
    parts.append("<table><tr><th>Field</th>")
    for m in MODELS:
        if m in outlines:
            parts.append(f"<th>{esc(outlines[m]['model_label'])}</th>")
    parts.append("</tr>")

    for field in [
        "title",
        "language",
        "source_type",
        "target_audience",
        "summary",
    ]:
        parts.append(f"<tr><td><b>{field}</b></td>")
        for m in MODELS:
            if m in outlines:
                val = outlines[m]["outline"].get(field, "")
                parts.append(f"<td>{esc(str(val)[:500])}</td>")
        parts.append("</tr>")

    parts.append("<tr><td><b>topics</b></td>")
    for m in MODELS:
        if m in outlines:
            topics = outlines[m]["outline"].get("topics", [])
            parts.append(f"<td>{esc(', '.join(topics))}</td>")
    parts.append("</tr>")

    parts.append("<tr><td><b>tools</b></td>")
    for m in MODELS:
        if m in outlines:
            tools = outlines[m]["outline"].get("tools", [])
            parts.append(f"<td>{esc(', '.join(tools))}</td>")
    parts.append("</tr>")

    parts.append("<tr><td><b>presenter</b></td>")
    for m in MODELS:
        if m in outlines:
            p = outlines[m]["outline"].get("presenter", {})
            parts.append(
                f"<td>{esc(p.get('description', ''))}<br>"
                f"<i>Style: {esc(p.get('style', ''))}</i><br>"
                f"<i>{esc(p.get('delivery_notes', ''))}</i></td>"
            )
    parts.append("</tr>")

    parts.append("<tr><td><b>prerequisites</b></td>")
    for m in MODELS:
        if m in outlines:
            prereqs = outlines[m]["outline"].get("prerequisites", [])
            parts.append(
                f"<td>{esc(', '.join(prereqs)) if prereqs else '<i>none</i>'}</td>"
            )
    parts.append("</tr></table>")

    # === 4. PER-MODEL SECTIONS ===
    parts.append("<h2>4. Content Layer (Sections) — Per Model</h2>")
    for m in MODELS:
        if m not in outlines:
            continue
        o = outlines[m]
        ol = o["outline"]
        parts.append(
            f"<h3>{esc(o['model_label'])} — {len(ol.get('sections', []))} section(s)</h3>"
        )
        parts.append('<div class="model-block">')

        for i, sec in enumerate(ol.get("sections", [])):
            time_range = f"{sec['start_sec']:.0f}s – {sec['end_sec']:.0f}s"
            parts.append('<div class="section-block">')
            parts.append(
                f"<h4>Section {i + 1}: {esc(sec['title'])} "
                f'<span class="time-badge">{time_range}</span></h4>'
            )
            parts.append("<div><b>Narration:</b></div>")
            parts.append(f'<div class="narration">{esc(sec["narration"])}</div>')

            if sec.get("screen_content"):
                parts.append("<div><b>Screen:</b></div>")
                parts.append(f'<div class="screen">{esc(sec["screen_content"])}</div>')

            for snippet in sec.get("code_snippets", []):
                parts.append(
                    f'<div style="color:#aaa; font-size:12px; margin-top:6px;">'
                    f"<b>Code</b> ({esc(snippet.get('language', ''))}) — "
                    f"{esc(snippet.get('context', ''))}</div>"
                )
                parts.append(f'<div class="code-block">{esc(snippet["code"])}</div>')

            if sec.get("key_concepts"):
                parts.append('<div style="margin-top:6px;"><b>Key concepts:</b></div>')
                parts.append('<div class="concepts">')
                for c in sec["key_concepts"]:
                    parts.append(f'<span class="concept-tag">{esc(c)}</span>')
                parts.append("</div>")
            parts.append("</div>")

        parts.append("</div>")

    # === 5. LOSSLESS CHECK ===
    parts.append("<h2>5. Lossless Check: Raw STT vs Outline Narration</h2>")
    parts.append(
        '<p style="color:#888;">Side-by-side: raw STT (left, red) vs outline '
        "narration (right, green). Verify every thesis/example is preserved.</p>"
    )
    for m in MODELS:
        if m not in outlines:
            continue
        o = outlines[m]
        ol = o["outline"]
        parts.append(f"<h3>{esc(o['model_label'])}</h3>")
        parts.append('<div class="compare-grid">')

        parts.append("<div>")
        parts.append('<h4 style="color:#e94560;">Raw STT</h4>')
        for seg in stt["segments"]:
            parts.append(
                f'<div class="raw-block">'
                f'<span class="time-badge">'
                f"{seg['start_sec']:.1f}s–{seg['end_sec']:.1f}s</span>"
                f'<div class="raw-text">{esc(seg["text"])}</div></div>'
            )
        parts.append("</div>")

        parts.append("<div>")
        parts.append('<h4 style="color:#4ecca3;">Outline Narration</h4>')
        for sec in ol.get("sections", []):
            parts.append(
                f'<div class="section-block">'
                f'<span class="time-badge">'
                f"{sec['start_sec']:.0f}s–{sec['end_sec']:.0f}s</span> "
                f"<b>{esc(sec['title'])}</b>"
                f'<div class="narration">{esc(sec["narration"])}</div></div>'
            )
        parts.append("</div>")
        parts.append("</div>")

    parts.append("</body></html>")

    report_path = OUT_DIR / report_name
    report_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    title = sys.argv[2] if len(sys.argv) > 2 else slug or "unknown"
    name = sys.argv[3] if len(sys.argv) > 3 else "report.html"
    if slug:
        generate_report(slug, title, name)
    else:
        print("Usage: python _report_outline.py <slug> <title> <report_name>")
