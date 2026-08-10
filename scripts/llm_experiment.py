"""LLM token/quality experiment: which model setting gives min tokens at max quality?

Runs 3 Z.AI model variants over a sample of ~20 hard books (NEEDS_REVIEW /
CONTENT_MISMATCH with usable first-page text) from the library, and reports per
model: avg input tokens, avg output tokens, avg reasoning tokens, total wall
time, and the parsed result so you can eyeball quality.

API key is loaded exactly like the production code (Config.from_env() ->
ZAI_API_KEY from .env / env), so the key never appears on the command line.

Usage:
    .venv/bin/python scripts/llm_experiment.py [--limit N] [--sample-by {detect|verify}]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# Make the package importable when run from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from book_meta_fix.config import Config  # noqa: E402
from book_meta_fix.detectors import detect as detect_fn  # noqa: E402
from book_meta_fix.extractors import extract  # noqa: E402
from book_meta_fix.library import scan_library  # noqa: E402
from book_meta_fix.llm import SYSTEM_PROMPT, ReconciledMeta, _parse_llm_json, build_user_prompt  # noqa: E402
from book_meta_fix.models import Verdict  # noqa: E402


# The three variants under test. Each entry produces the kwargs passed to
# openai.chat.completions.create (plus a fixed model). `extra_body` carries
# Z.AI-specific fields the OpenAI client forwards verbatim.
@dataclass
class Variant:
    label: str
    model: str
    max_tokens: int
    extra_body: dict[str, Any] = field(default_factory=dict)


VARIANTS = [
    Variant("glm-5.2_reasoning-low", "glm-5.2", max_tokens=8000, extra_body={"reasoning_effort": "low"}),
    Variant("glm-4.6_thinking-off", "glm-4.6", max_tokens=4000, extra_body={"thinking": {"type": "disabled"}}),
    Variant("glm-4.5-air_thinking-off", "glm-4.5-air", max_tokens=4000, extra_body={"thinking": {"type": "disabled"}}),
    # Flash models are listed as Free on Z.AI pricing — potentially ideal cheap
    # fallback if CZ/SK quality holds. Run with thinking off (Flash models are
    # typically non-reasoning by default, but pass the flag for parity).
    Variant("glm-4.5-flash", "glm-4.5-flash", max_tokens=4000, extra_body={"thinking": {"type": "disabled"}}),
    Variant("glm-4.7-flash", "glm-4.7-flash", max_tokens=4000, extra_body={"thinking": {"type": "disabled"}}),
]


@dataclass
class CallResult:
    ok: bool
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0  # includes reasoning tokens on Z.AI
    reasoning_chars: int = 0
    content_tokens: int = 0  # completion_tokens minus reasoning (best-effort)
    wall_seconds: float = 0.0
    parsed: ReconciledMeta | None = None
    raw_content: str = ""


def call_variant(client: Any, variant: Variant, evidence: dict) -> CallResult:
    prompt = build_user_prompt(evidence)
    start = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=variant.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=variant.max_tokens,
            extra_body=variant.extra_body or None,
        )
    except Exception as e:  # noqa: BLE001
        return CallResult(ok=False, error=f"{type(e).__name__}: {e}", wall_seconds=time.monotonic() - start)

    wall = time.monotonic() - start
    choice = resp.choices[0]
    content = choice.message.content or ""
    reasoning = getattr(choice.message, "reasoning_content", None) or ""
    usage = getattr(resp, "usage", None)
    ptoks = getattr(usage, "prompt_tokens", 0) if usage else 0
    ctoks = getattr(usage, "completion_tokens", 0) if usage else 0
    # Z.AI counts reasoning tokens inside completion_tokens on reasoning models.
    # We also keep the raw reasoning length for a ground-truth-ish size signal.
    parsed = _parse_llm_json(content) if content.strip() else None
    return CallResult(
        ok=True,
        prompt_tokens=ptoks,
        completion_tokens=ctoks,
        reasoning_chars=len(reasoning),
        wall_seconds=wall,
        parsed=parsed,
        raw_content=content[:400],
    )


def select_hard_books(cfg: Config, limit: int) -> list[tuple[Any, Any, Any]]:
    """Return up to *limit* (meta, diag, extracted) tuples for books the LLM
    would actually be asked about: NEEDS_REVIEW/AUTO_FIXABLE with usable
    first-page text. Uses the detector + a quick extract, no enrichment.
    """
    books = scan_library(cfg.library, cache=None)
    out: list[tuple[Any, Any, Any]] = []
    for meta in books:
        diag = detect_fn(meta)
        if diag.verdict == Verdict.OK:
            continue
        if not meta.primary_file:
            continue
        try:
            extracted = extract(meta.primary_file)
        except Exception:  # noqa: BLE001
            continue
        if not extracted or not extracted.first_page_text:
            continue
        out.append((meta, diag, extracted))
        if len(out) >= limit:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20, help="number of hard books to sample (default 20)")
    args = ap.parse_args()

    cfg = Config.from_env()
    if not cfg.zai_api_key:
        print("ERROR: ZAI_API_KEY not configured (set it in .env or env).", file=sys.stderr)
        return 2

    print(f"Sampling up to {args.limit} hard books from {cfg.library} ...", file=sys.stderr)
    sample = select_hard_books(cfg, args.limit)
    if not sample:
        print("No usable hard books found (need NEEDS_REVIEW + primary_file + first_page_text).", file=sys.stderr)
        return 1
    print(f"Selected {len(sample)} books. Building evidence ...", file=sys.stderr)

    # Build the evidence once per book (same prompt across variants).
    from book_meta_fix.pipeline import _build_llm_evidence  # noqa: E402

    evidences = []
    for meta, diag, extracted in sample:
        evidences.append((_build_llm_evidence(meta, diag, extracted), meta, diag))

    # Lazy-init the OpenAI client (same pattern as ZaiProvider._get_client).
    try:
        from openai import OpenAI
    except ImportError:
        print("openai package not installed", file=sys.stderr)
        return 2
    client = OpenAI(api_key=cfg.zai_api_key, base_url=cfg.zai_base_url)

    # Per-variant aggregate stats.
    agg: dict[str, dict[str, list[float]]] = {v.label: {"ptoks": [], "ctoks": [], "reasoning": [], "wall": [], "ok": []} for v in VARIANTS}
    per_book_rows: list[dict] = []

    for vi, variant in enumerate(VARIANTS):
        print(f"\n=== Variant {vi + 1}/{len(VARIANTS)}: {variant.label} ===", file=sys.stderr)
        for bi, (evidence, meta, diag) in enumerate(evidences):
            # Respect the production rate limit between calls.
            time.sleep(max(0.0, cfg.llm_min_interval))
            r = call_variant(client, variant, evidence)
            agg[variant.label]["ptoks"].append(r.prompt_tokens)
            agg[variant.label]["ctoks"].append(r.completion_tokens)
            agg[variant.label]["reasoning"].append(r.reasoning_chars)
            agg[variant.label]["wall"].append(r.wall_seconds)
            agg[variant.label]["ok"].append(1.0 if r.ok else 0.0)
            row = {
                "book": bi,
                "variant": variant.label,
                "category": diag.category,
                "ok": r.ok,
                "error": r.error,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "reasoning_chars": r.reasoning_chars,
                "wall_s": round(r.wall_seconds, 2),
                "parsed_title": (r.parsed.title if r.parsed else None),
                "parsed_authors": (r.parsed.authors if r.parsed else None),
                "parsed_isbn": (r.parsed.isbn if r.parsed else None),
            }
            per_book_rows.append(row)
            status = "ok" if r.ok else f"ERR {r.error[:40]}"
            print(f"  book {bi + 1}/{len(evidences)} [{diag.category}] ptoks={r.prompt_tokens} ctoks={r.completion_tokens} reason={r.reasoning_chars}c {r.wall_seconds:.1f}s {status}", file=sys.stderr)

    # --- Aggregate summary ---
    print("\n" + "=" * 100)
    print("AGGREGATE SUMMARY (per variant, averaged over books)")
    print("=" * 100)
    print(f"{'variant':<30} {'n':>4} {'ok%':>6} {'ptoks':>8} {'ctoks':>8} {'reason_c':>9} {'wall_s':>8}")
    for v in VARIANTS:
        a = agg[v.label]
        n = len(a["ok"])
        ok_pct = 100.0 * (sum(a["ok"]) / n) if n else 0.0
        ptoks = statistics.mean(a["ptoks"]) if a["ptoks"] else 0.0
        ctoks = statistics.mean(a["ctoks"]) if a["ctoks"] else 0.0
        reason = statistics.mean(a["reasoning"]) if a["reasoning"] else 0.0
        wall = statistics.mean(a["wall"]) if a["wall"] else 0.0
        print(f"{v.label:<30} {n:>4} {ok_pct:>5.0f}% {ptoks:>8.0f} {ctoks:>8.0f} {reason:>9.0f} {wall:>8.1f}")

    # Dump per-book rows as JSON for later inspection / quality comparison.
    print("\n# per-book JSON (for quality eyeballing):")
    print(json.dumps(per_book_rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
