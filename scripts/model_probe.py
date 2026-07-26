#!/usr/bin/env python
"""Diagnostic probe — Alibaba DashScope model availability + reasoning controllability.

Zero step of the vision-model replacement spike (see
``refactoring-vision/sprint/tasks/vision-model-spike/PRE-PLAN.md``). The tool is
strictly read-only: it makes external DashScope calls and prints. It writes
NOTHING to the database, creates no ``Job`` rows, and changes no system state.

For every candidate model it runs three single-image vision calls that reproduce
the production presentation Pass-1 payload byte-for-byte — the same prompt
(``prompts/presentation_pass_1_vision/v1.md``), the same slide render (PyMuPDF
@150 dpi), the same message shape (``DashScopeProvider._build_messages``):

  A  baseline  — no reasoning / thinking parameter at all (model default)
  B  our form  — ``DashScopeProvider.complete`` with
                 ``request.reasoning={"exclude": True}`` (the live ladder form)
  C  native    — raw ``AioMultiModalConversation.call(enable_thinking=False)``,
                 bypassing our connector

Three states are named explicitly for every call; a HTTP 200 is NEVER read as
success on its own:

  * took effect      — HTTP 200 and NO ``reasoning_content`` on the message
  * silently ignored — HTTP 200 but ``reasoning_content`` is populated
                       (the most dangerous state — indistinguishable from success
                       by status code alone)
  * rejected         — non-200 response / exception from the server

Ground truth behind the design (dashscope==1.25.18, verified against the installed
package): the multimodal branch has NO first-class ``enable_thinking`` / ``reasoning``
parameter — any unknown kwarg is forwarded verbatim into the request envelope's
top-level ``parameters`` object and decided server-side. Our ``DashScopeProvider``
discards the raw usage object and never reads ``reasoning_content``; mode B
therefore tees the raw SDK response so the suppression can actually be observed on
the production path.

Run (operator only — this file is not executed by the author):

    uv run python scripts/model_probe.py
    uv run python scripts/model_probe.py \\
        --models qwen3.5-flash qwen3.6-flash qwen3-vl-32b-instruct --slide 5

Requires the live environment (``ALIBABA_API_KEY`` / ``DASHSCOPE_BASE_URL`` for the
eu-central-1 / Frankfurt MaaS workspace).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import dashscope.aigc.multimodal_conversation as _mm
import fitz  # PyMuPDF — same renderer as ingestion/presentation.py
import yaml

from course_supporter.config import get_settings
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.providers.dashscope import DashScopeProvider
from course_supporter.llm.schemas import LLMRequest

# ── Constants mirrored from the production presentation pipeline ──────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROMPT_REF = "prompts/presentation_pass_1_vision/v1.md"
_REGISTRY_PATH = _REPO_ROOT / "config" / "external_services.yaml"
_RENDER_DPI = 150  # presentation.py:_RENDER_DPI

_DEFAULT_MATERIAL = _REPO_ROOT / "tests/fixtures/presentations/lesson6_functions_1.pdf"
_DEFAULT_SLIDE = 5  # the code-heavy reference slide (PRE-PLAN §Метод калібрування)
_DEFAULT_MODELS = ["qwen3.5-flash", "qwen3.6-flash", "qwen3-vl-32b-instruct"]
_DEFAULT_MAX_TOKENS = 8192  # production presentation_pass_1_vision effective ceiling

# The current model — guaranteed 0 reasoning tokens (phase 2.3). It is the
# positive control for the tool's own reasoning detector: any non-zero reasoning
# on it means the DETECTOR is broken, not the model.
_POSITIVE_CONTROL = "qwen3-vl-32b-instruct"

# Run-budget reminder (PRE-PLAN §Бюджет прогону). This probe is nine tiny calls,
# but the ceiling is stated so the number is never lost.
_BUDGET_HARD_USD = 10.0
_BUDGET_ESCALATE_USD = 7.0


@dataclass
class CallResult:
    """Everything observed for a single (model, mode) call — no interpretation."""

    status_code: int | None = None
    code: str | None = None
    message: str | None = None
    visible_chars: int = 0
    reasoning_chars: int = 0
    raw_usage: Any = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    error: str | None = None
    note: str | None = None


@dataclass
class ModelRun:
    """The three modes for one model."""

    model: str
    a: CallResult = field(default_factory=CallResult)
    b: CallResult = field(default_factory=CallResult)
    c: CallResult = field(default_factory=CallResult)


# ── Rendering + prompt (production-faithful) ─────────────────────────────────


def render_slide(material: Path, slide_number: int) -> tuple[bytes, str]:
    """Render one 1-indexed slide to PNG bytes + its text layer.

    Mirrors ``PresentationProcessor._extract_pdf_pages`` (PyMuPDF, @150 dpi,
    ``pixmap.tobytes("png")``, ``page.get_text("text").strip()``).
    """
    doc = fitz.open(material)
    try:
        idx = slide_number - 1
        if idx < 0 or idx >= doc.page_count:
            raise SystemExit(
                f"slide {slide_number} out of range 1..{doc.page_count} for {material}"
            )
        page = doc.load_page(idx)
        raw_text = page.get_text("text").strip()
        png = page.get_pixmap(dpi=_RENDER_DPI).tobytes("png")
        return png, raw_text
    finally:
        doc.close()


def render_prompt(slide_number: int, raw_text: str) -> str:
    """Load + render the exact production Pass-1 prompt (no rewriting)."""
    prompt = load_prompt(_PROMPT_REF, base_path=_REPO_ROOT).render(
        slide_number=slide_number,
        raw_text=raw_text,
    )
    return prompt.user or ""


def base_request(model: str, user_text: str, png: bytes, max_tokens: int) -> LLMRequest:
    """The shared request; only the reasoning/thinking knob differs per mode."""
    return LLMRequest(
        prompt=user_text,
        system_prompt=None,  # Pass-1 has no system section
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
        contents=[png],
        reasoning=None,
        expects_json=False,
    )


# ── Response inspection (server-side is the only judge) ──────────────────────


def _extract_reasoning(resp: Any) -> str:
    """Return the message ``reasoning_content`` string, or ``""``.

    ``reasoning_content`` is a server-only field on ``choices[0].message`` (a
    dict subclass). It is absent unless the model actually emitted thinking, so
    ``.get`` (never attribute access, which raises ``KeyError`` here) is used.
    """
    try:
        output = resp.output
        choices = output.get("choices") if hasattr(output, "get") else None
        if not choices:
            return ""
        first = choices[0]
        msg = first.get("message") if hasattr(first, "get") else None
        if msg is None:
            return ""
        value = msg.get("reasoning_content") if hasattr(msg, "get") else None
        return value or ""
    except Exception:
        return ""


def inspect_response(
    provider: DashScopeProvider, resp: Any, latency_ms: int
) -> CallResult:
    """Turn a raw DashScope SDK response into a CallResult (no interpretation)."""
    status = getattr(resp, "status_code", None)
    if status != 200:
        return CallResult(
            status_code=status,
            code=getattr(resp, "code", None) or "",
            message=getattr(resp, "message", None) or "",
            latency_ms=latency_ms,
        )
    try:
        visible = provider._extract_text(resp)  # production extractor, identical text
    except Exception:
        visible = ""
    reasoning = _extract_reasoning(resp)
    usage = getattr(resp, "usage", None)
    tokens_in = usage.get("input_tokens") if usage else None
    tokens_out = usage.get("output_tokens") if usage else None
    return CallResult(
        status_code=200,
        visible_chars=len(visible),
        reasoning_chars=len(reasoning),
        raw_usage=usage,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
    )


# ── The three call modes ─────────────────────────────────────────────────────


async def call_raw(
    provider: DashScopeProvider,
    model: str,
    messages: list[dict[str, Any]],
    api_key: str,
    max_tokens: int,
    thinking_kwargs: dict[str, Any],
) -> CallResult:
    """Modes A and C — raw ``AioMultiModalConversation.call`` (connector bypassed)."""
    t0 = time.perf_counter()
    try:
        resp = await _mm.AioMultiModalConversation.call(
            model=model,
            api_key=api_key,
            messages=messages,
            temperature=0.0,
            max_tokens=max_tokens,
            **thinking_kwargs,
        )
    except Exception as exc:
        return CallResult(
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
    return inspect_response(provider, resp, int((time.perf_counter() - t0) * 1000))


async def call_provider(provider: DashScopeProvider, request: LLMRequest) -> CallResult:
    """Mode B — the real production path (``DashScopeProvider.complete``) with
    ``reasoning={"exclude": True}``, teeing the raw SDK response.

    The connector discards the raw usage object and never reads
    ``reasoning_content``, so we temporarily wrap ``AioMultiModalConversation.call``
    to capture the untouched response while the genuine provider code runs (key
    rotation, message build, error mapping). One SDK call — the wrapper only
    observes.
    """
    request = request.model_copy(update={"reasoning": {"exclude": True}})
    captured: dict[str, Any] = {}
    real_call = _mm.AioMultiModalConversation.call

    async def _tee(*args: Any, **kwargs: Any) -> Any:
        resp = await real_call(*args, **kwargs)
        captured["resp"] = resp
        return resp

    t0 = time.perf_counter()
    provider_error: str | None = None
    with patch.object(_mm.AioMultiModalConversation, "call", _tee):
        try:
            await provider.complete(request)
        except Exception as exc:
            provider_error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.perf_counter() - t0) * 1000)

    resp = captured.get("resp")
    if resp is None:
        return CallResult(
            error=provider_error or "no response captured before failure",
            latency_ms=latency_ms,
        )
    result = inspect_response(provider, resp, latency_ms)
    if provider_error and result.status_code == 200:
        # 200 on the wire but the connector still raised — worth surfacing.
        result.note = f"connector raised on a 200 response: {provider_error}"
    return result


# ── Classification + cost ────────────────────────────────────────────────────


def classify(res: CallResult) -> str:
    """Name the raw state — a 200 is never assumed to be success."""
    if res.error:
        return "REJECTED"
    if res.status_code != 200:
        return "REJECTED"
    if res.reasoning_chars > 0:
        return "REASONING PRESENT"
    return "NO REASONING"


def interpret(mode: str, res: CallResult) -> str:
    """Explicit verdict for one call, distinguishing the three states."""
    if res.error:
        return f"REJECTED — exception ({res.error})"
    if res.status_code != 200:
        return f"REJECTED — server {res.status_code} {res.code}: {res.message}"
    if mode == "A":
        return (
            "reasons by DEFAULT (reasoning tokens present)"
            if res.reasoning_chars > 0
            else "no reasoning by default"
        )
    # Modes B and C are suppression attempts.
    if res.reasoning_chars > 0:
        return "SILENTLY IGNORED — HTTP 200 but reasoning_content present (danger)"
    return "took EFFECT — reasoning suppressed"


def load_rates(registry_path: Path) -> dict[str, tuple[float, float]]:
    """Map model id -> (cost_per_1k_in, cost_per_1k_out) from the registry."""
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    rates: dict[str, tuple[float, float]] = {}
    for provider in (data.get("providers") or {}).values():
        for model in provider.get("models") or []:
            if "cost_per_1k_in" in model and "cost_per_1k_out" in model:
                rates[model["id"]] = (
                    float(model["cost_per_1k_in"]),
                    float(model["cost_per_1k_out"]),
                )
    return rates


def call_cost(
    model: str, res: CallResult, rates: dict[str, tuple[float, float]]
) -> float | None:
    """Per-call USD cost, or ``None`` when the model has no registry rate."""
    rate = rates.get(model)
    if rate is None or res.tokens_in is None or res.tokens_out is None:
        return None
    cost_in, cost_out = rate
    return (res.tokens_in / 1000) * cost_in + (res.tokens_out / 1000) * cost_out


# ── Printing ─────────────────────────────────────────────────────────────────


def _fmt_usage(res: CallResult) -> str:
    if res.raw_usage is None:
        return "  (no usage — call did not return 200)"
    try:
        dumped = json.dumps(res.raw_usage, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        dumped = repr(res.raw_usage)
    return "\n".join("  " + line for line in dumped.splitlines())


def print_call(
    model: str, mode: str, label: str, res: CallResult, cost: float | None
) -> None:
    print(f"\n  ── mode {mode}: {label}")
    if res.error:
        print(f"     status        : ERROR — {res.error}")
        print(f"     latency_ms    : {res.latency_ms}")
        print(f"     >>> STATE     : {interpret(mode, res)}")
        return
    print(f"     status_code   : {res.status_code}", end="")
    if res.status_code != 200:
        print(f"  (code={res.code!r} message={res.message!r})")
        print(f"     latency_ms    : {res.latency_ms}")
        print(f"     >>> STATE     : {interpret(mode, res)}")
        return
    print()
    present = res.reasoning_chars > 0
    print(f"     reasoning     : present={present} chars={res.reasoning_chars}")
    print(f"     visible_chars : {res.visible_chars}")
    print(f"     latency_ms    : {res.latency_ms}")
    cost_txt = "n/a (no registry rate)" if cost is None else f"${cost:.6f}"
    print(f"     cost          : {cost_txt}")
    print(f"     tokens        : in={res.tokens_in} out={res.tokens_out}")
    if res.note:
        print(f"     note          : {res.note}")
    print("     raw usage (verbatim, no field selection):")
    print(_fmt_usage(res))
    print(f"     >>> STATE     : {interpret(mode, res)}")


def print_summary(runs: list[ModelRun], rates: dict[str, tuple[float, float]]) -> None:
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    header = (
        f"{'model':<26} {'mode':<5} {'state':<18} "
        f"{'reason_ch':>9} {'vis_ch':>7} {'ms':>6}"
    )
    print(header)
    print("-" * 78)
    for run in runs:
        for mode, res in (("A", run.a), ("B", run.b), ("C", run.c)):
            print(
                f"{run.model:<26} {mode:<5} {classify(res):<18} "
                f"{res.reasoning_chars:>9} {res.visible_chars:>7} {res.latency_ms:>6}"
            )
    print("-" * 78)
    # output_tokens side by side (A | B | C) — the cross-check for reasoning the
    # server may report under a name other than ``reasoning_content``: an empty
    # reasoning_content next to undiminished output_tokens is NOT a suppression.
    print("output_tokens per model (A | B | C):")
    for run in runs:
        a = "n/a" if run.a.tokens_out is None else str(run.a.tokens_out)
        b = "n/a" if run.b.tokens_out is None else str(run.b.tokens_out)
        c = "n/a" if run.c.tokens_out is None else str(run.c.tokens_out)
        print(f"{run.model:<26} A={a:>7}  B={b:>7}  C={c:>7}")
    print("-" * 78)


# A genuine reasoning suppression cuts output_tokens sharply; an empty
# ``reasoning_content`` alone is not trusted (the server may name the field
# differently, in which case output stays undiminished).
_TOKENS_DROP_THRESHOLD = 0.25
_INCONCLUSIVE_NOTE = (
    "INCONCLUSIVE — reasoning_content порожній, але output_tokens не впали; "
    "ймовірно інша назва поля, перевір сирий usage вручну"
)


def _tokens_dropped(res: CallResult, baseline: CallResult) -> bool | None:
    """Did ``res`` output_tokens fall >=25% vs the mode-A baseline?

    Returns ``None`` when unmeasurable (either side's ``tokens_out`` missing, or a
    non-positive baseline) — an unmeasurable cross-check never counts as a pass.
    """
    if (
        res.tokens_out is None
        or baseline.tokens_out is None
        or baseline.tokens_out <= 0
    ):
        return None
    return res.tokens_out <= (1.0 - _TOKENS_DROP_THRESHOLD) * baseline.tokens_out


def _suppression_outcome(res: CallResult, baseline: CallResult) -> str:
    """Cross-checked outcome of a suppression mode (B/C) against the A baseline.

    ``credited``     — 200, no reasoning_content, AND output_tokens dropped >=25%.
    ``inconclusive`` — 200, no reasoning_content, but the token cross-check does
                       not corroborate (no drop, or tokens unmeasurable).
    ``reasoning``    — 200 with reasoning_content present.
    ``rejected``     — non-200 / exception.
    """
    if res.error or res.status_code != 200:
        return "rejected"
    if res.reasoning_chars > 0:
        return "reasoning"
    return "credited" if _tokens_dropped(res, baseline) is True else "inconclusive"


def model_verdict(run: ModelRun) -> str:
    """Availability + which parameter (if any) suppresses reasoning.

    A suppression is credited only when ``reasoning_content`` is empty AND
    output_tokens fell >=25% vs mode A — the token drop guards against a false
    "suppressed" verdict when the server reports reasoning under another field
    name (empty reasoning_content next to undiminished output).
    """
    states = {classify(run.a), classify(run.b), classify(run.c)}
    if states == {"REJECTED"}:
        why = run.a.code or run.a.error or "all calls rejected"
        return f"UNAVAILABLE via native connector — every call rejected ({why})"

    b_out = _suppression_outcome(run.b, run.a)
    c_out = _suppression_outcome(run.c, run.a)

    # All three modes unmeasured: reasoning_content empty in A and neither
    # suppression mode corroborated by a token drop. Name it — do NOT read an
    # empty reasoning_content as "does not reason by default".
    a_unconfirmed = run.a.status_code == 200 and run.a.reasoning_chars == 0
    if a_unconfirmed and b_out == "inconclusive" and c_out == "inconclusive":
        return (
            "INCONCLUSIVE across all three modes — reasoning_content empty in "
            "A/B/C and output_tokens give no corroboration; the field is likely "
            "named differently. Inspect raw usage manually — do NOT conclude the "
            "model does not reason."
        )

    reasons_by_default = run.a.status_code == 200 and run.a.reasoning_chars > 0
    if not reasons_by_default and classify(run.a) != "REJECTED":
        b = interpret("B", run.b)
        c = interpret("C", run.c)
        return (
            f"does NOT reason by default — no suppression required. [B: {b}] [C: {c}]"
        )

    credited: list[str] = []
    if b_out == "credited":
        credited.append("reasoning={'exclude': True} (our connector form, mode B)")
    if c_out == "credited":
        credited.append("enable_thinking=False (native SDK form, mode C)")

    inconclusive = [
        label for label, out in (("B", b_out), ("C", c_out)) if out == "inconclusive"
    ]

    parts: list[str] = []
    if credited:
        parts.append("reasoning GASHED by: " + " AND ".join(credited))
    if inconclusive:
        parts.append(f"mode(s) {', '.join(inconclusive)}: {_INCONCLUSIVE_NOTE}")
    if not credited and not inconclusive:
        parts.append(
            "NOT GASHED by any tested parameter — fails the controllability gate "
            "(PRE-PLAN §Метод калібрування); candidate is disqualified "
            "regardless of price"
        )
    return " | ".join(parts)


# ── Orchestration ────────────────────────────────────────────────────────────


async def run_model(
    provider: DashScopeProvider,
    model: str,
    user_text: str,
    png: bytes,
    api_key: str,
    max_tokens: int,
    rates: dict[str, tuple[float, float]],
) -> ModelRun:
    print("\n" + "#" * 78)
    print(f"# MODEL: {model}")
    print("#" * 78)
    run = ModelRun(model=model)

    request = base_request(model, user_text, png, max_tokens)
    messages = provider._build_messages(request)  # identical wire payload for A/C

    # A — baseline, no reasoning/thinking parameter.
    run.a = await call_raw(provider, model, messages, api_key, max_tokens, {})
    print_call(
        model,
        "A",
        "baseline (no reasoning/thinking parameter)",
        run.a,
        call_cost(model, run.a, rates),
    )

    # B — our connector form: reasoning={"exclude": True} via DashScopeProvider.
    run.b = await call_provider(provider, request)
    print_call(
        model,
        "B",
        "DashScopeProvider.complete, reasoning={'exclude': True} "
        "(wire parameters.reasoning)",
        run.b,
        call_cost(model, run.b, rates),
    )

    # C — native SDK form: enable_thinking=False (wire parameters.enable_thinking).
    run.c = await call_raw(
        provider, model, messages, api_key, max_tokens, {"enable_thinking": False}
    )
    print_call(
        model,
        "C",
        "raw SDK AioMultiModalConversation.call(enable_thinking=False) "
        "(wire parameters.enable_thinking)",
        run.c,
        call_cost(model, run.c, rates),
    )

    return run


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DashScope model availability + reasoning-controllability probe."
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=_DEFAULT_MODELS,
        help="Canonical ids WITHOUT date suffix (dated forms return AccessDenied).",
    )
    parser.add_argument(
        "--material",
        type=Path,
        default=_DEFAULT_MATERIAL,
        help="Path to the source presentation (PDF).",
    )
    parser.add_argument(
        "--slide",
        type=int,
        default=_DEFAULT_SLIDE,
        help="1-indexed slide number to render.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=_DEFAULT_MAX_TOKENS,
        help="max_tokens per call (production Pass-1 uses 8192).",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()

    settings = get_settings()
    pool = settings.key_pool_for("dashscope")
    if pool is None:
        raise SystemExit(
            "No DashScope credentials configured (ALIBABA_API_KEY). "
            "This probe needs the live Frankfurt MaaS workspace."
        )
    api_keys = [k.get_secret_value() for k in pool.all_keys()]
    provider = DashScopeProvider(
        api_keys=api_keys,
        default_model=settings.dashscope_default_model,
        base_url=settings.dashscope_base_url,
    )
    api_key = api_keys[0]

    png, raw_text = render_slide(args.material, args.slide)
    user_text = render_prompt(args.slide, raw_text)
    rates = load_rates(_REGISTRY_PATH)

    print("=" * 78)
    print("DashScope reasoning-controllability probe (zero step, vision-model spike)")
    print("=" * 78)
    print(f"base_url        : {settings.dashscope_base_url}")
    print(f"material        : {args.material}  (slide {args.slide})")
    print(f"prompt          : {_PROMPT_REF}  (production Pass-1, rendered verbatim)")
    print(f"png bytes       : {len(png)}   text-layer chars: {len(raw_text)}")
    print(f"max_tokens      : {args.max_tokens}   temperature: 0.0")
    print(f"models          : {', '.join(args.models)}")
    print(f"total calls     : {len(args.models)} x 3 = {len(args.models) * 3}")
    print(
        "state legend    : took-effect = 200 & no reasoning_content | "
        "silently-ignored = 200 & reasoning_content present | "
        "rejected = non-200/exception"
    )

    runs: list[ModelRun] = []
    for model in args.models:
        runs.append(
            await run_model(
                provider, model, user_text, png, api_key, args.max_tokens, rates
            )
        )

    print_summary(runs, rates)

    # ── Positive-control sanity: qwen3-vl-32b-instruct must show 0 reasoning ──
    broken = False
    for run in runs:
        if run.model != _POSITIVE_CONTROL:
            continue
        offenders = [
            mode
            for mode, res in (("A", run.a), ("B", run.b), ("C", run.c))
            if res.status_code == 200 and res.reasoning_chars > 0
        ]
        if offenders:
            broken = True
            print("\n" + "!" * 78)
            print("!! POSITIVE CONTROL FAILED — STOP")
            print(
                f"!! {_POSITIVE_CONTROL} emitted reasoning tokens in mode(s) "
                f"{offenders}, but it is guaranteed NOT to reason (phase 2.3 = 0)."
            )
            print("!! The tool's reasoning detector is SUSPECT. Do NOT trust any")
            print("!! verdict above until this is explained. Report to operator.")
            print("!" * 78)

    # ── Per-model verdicts ────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("VERDICTS")
    print("=" * 78)
    for run in runs:
        marker = "  (positive control)" if run.model == _POSITIVE_CONTROL else ""
        print(f"\n{run.model}{marker}")
        print(f"  {model_verdict(run)}")

    # ── Cost roll-up ──────────────────────────────────────────────────────────
    total = 0.0
    unknown: list[str] = []
    for run in runs:
        model_known = run.model in rates
        if not model_known:
            unknown.append(run.model)
        for res in (run.a, run.b, run.c):
            cost = call_cost(run.model, res, rates)
            if cost is not None:
                total += cost
    print("\n" + "=" * 78)
    print("COST")
    print("=" * 78)
    print(f"total (models with a registry rate): ${total:.6f}")
    if unknown:
        print(
            "no registry rate (cost unknown, PRE-PLAN §Невідомі #3): "
            + ", ".join(sorted(set(unknown)))
        )
    print(
        f"run-budget reminder: hard cap ${_BUDGET_HARD_USD:.0f}, escalate past "
        f"${_BUDGET_ESCALATE_USD:.0f} (PRE-PLAN §Бюджет прогону)"
    )

    return 2 if broken else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
