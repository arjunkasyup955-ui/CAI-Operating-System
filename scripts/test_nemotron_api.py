"""Standalone NVIDIA Nemotron API verification script.

TEMPORARY, standalone - does not import or modify any AFOS module (no
core.model_router, no workflows, no agents). Reads NVIDIA_API_KEY from the
existing .env and talks to NVIDIA's NIM API directly via the OpenAI Python
client (NIM's own recommended integration path, since its endpoint is
OpenAI-compatible - no new SDK dependency needed, `openai` is already
installed in this project's venv).

Endpoint: https://integrate.api.nvidia.com/v1 (OpenAI-compatible
/chat/completions and /models surfaces). Auth: Bearer <NVIDIA_API_KEY>.

Run: python scripts/test_nemotron_api.py
"""

import json
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

import os  # noqa: E402

from openai import OpenAI  # noqa: E402

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
FALLBACK_MODEL_CANDIDATES = [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "nvidia/nemotron-4-340b-instruct",
]

results: dict[str, dict] = {}


def record(name: str, status: str, detail: str = "") -> None:
    results[name] = {"status": status, "detail": detail}
    print(f"[{status}] {name}{' - ' + detail if detail else ''}")


def get_client() -> OpenAI:
    api_key = os.environ.get("NVIDIA_API_KEY", "")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY not found in .env")
    return OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key)


def discover_nemotron_model(client: OpenAI) -> str:
    """Queries /v1/models to find a currently-available Nemotron model ID,
    rather than hardcoding a guess that may be stale against NVIDIA's
    catalog. Falls back to a short candidate list if listing fails/is empty.
    """
    try:
        models = client.models.list()
        ids = [m.id for m in models.data]
        nemotron_ids = sorted(m for m in ids if "nemotron" in m.lower())
        if nemotron_ids:
            print(f"  (discovered {len(nemotron_ids)} Nemotron model(s) via /v1/models, using: {nemotron_ids[0]})")
            return nemotron_ids[0]
        print("  (/v1/models returned no nemotron-* models; falling back to candidate list)")
    except Exception as exc:
        print(f"  (/v1/models discovery failed: {exc}; falling back to candidate list)")
    return FALLBACK_MODEL_CANDIDATES[0]


def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in response")
    return json.loads(match.group(0))


def main() -> int:
    print("=" * 72)
    print("NVIDIA Nemotron API Standalone Verification")
    print("=" * 72)

    # --- A. Connection Test -------------------------------------------- #
    print("\n== A. Connection Test ==")
    try:
        client = get_client()
        model = discover_nemotron_model(client)
        probe = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly one word: OK"}],
            max_tokens=10,
            timeout=30.0,
        )
        content = (probe.choices[0].message.content or "").strip()
        record("Connection Test", "PASS", f"model={model}, response={content!r}")
    except Exception as exc:
        record("Connection Test", "FAIL", str(exc))
        print("\nConnection failed - remaining tests cannot run meaningfully. Stopping.")
        print_final_report(model=None)
        return 1

    # --- B. Response Time Test ------------------------------------------ #
    print("\n== B. Response Time Test ==")
    try:
        start = time.perf_counter()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly one word: OK"}],
            max_tokens=10,
            timeout=60.0,
        )
        elapsed = time.perf_counter() - start
        record("Response Time Test", "PASS", f"{elapsed:.2f}s (response={resp.choices[0].message.content!r})")
    except Exception as exc:
        record("Response Time Test", "FAIL", str(exc))

    # --- C. JSON Output Test --------------------------------------------- #
    print("\n== C. JSON Output Test ==")
    json_prompt = (
        'Return ONLY valid JSON:\n'
        '{\n'
        '  "market":"AI Voice Agents",\n'
        '  "score":8.5,\n'
        '  "reason":"test"\n'
        '}'
    )
    try:
        start = time.perf_counter()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": json_prompt}],
            max_tokens=200,
            timeout=60.0,
        )
        elapsed = time.perf_counter() - start
        raw = resp.choices[0].message.content or ""
        stripped = raw.strip()
        has_markdown_fence = "```" in stripped
        is_pure_json = False
        try:
            json.loads(stripped)
            is_pure_json = True
        except Exception:
            pass
        parsed = extract_json(raw)
        no_extra_text = is_pure_json  # only true if the ENTIRE response was valid JSON, nothing surrounding it
        detail = (
            f"{elapsed:.2f}s, valid_json={parsed is not None}, "
            f"no_extra_reasoning_text={no_extra_text}, no_markdown={not has_markdown_fence}, raw={raw[:300]!r}"
        )
        if parsed is not None and not has_markdown_fence:
            record("JSON Output Test", "PASS", detail)
        else:
            record("JSON Output Test", "FAIL", detail)
    except Exception as exc:
        record("JSON Output Test", "FAIL", str(exc))

    # --- D. Long Prompt Test --------------------------------------------- #
    print("\n== D. Long Prompt Test ==")
    filler_sentence = (
        "AI voice agents are transforming how small and medium businesses in India handle "
        "inbound customer calls, appointment scheduling, and lead qualification without "
        "requiring a dedicated human receptionist. "
    )
    long_content = (filler_sentence * ((3500 // len(filler_sentence)) + 1))[:3500]
    long_prompt = (
        "You are a market analyst. Based ONLY on the research content below, produce a "
        "JSON object with exactly these keys: market_size_tam (string), cagr (string), "
        "market_segments (list of strings), growth_drivers (list of strings), "
        "confidence_score (float 0.0-1.0). Respond with ONLY the JSON object, no other text.\n\n"
        f"Business idea: AI Voice Agent for Indian SMEs\n\nResearch content:\n{long_content}"
    )
    print(f"  (generated prompt length: {len(long_prompt)} chars)")
    try:
        start = time.perf_counter()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": long_prompt}],
            max_tokens=500,
            timeout=120.0,
        )
        elapsed = time.perf_counter() - start
        raw = resp.choices[0].message.content or ""
        try:
            extract_json(raw)
            quality = "valid JSON produced"
        except Exception as parse_exc:
            quality = f"NOT valid JSON: {parse_exc}"
        record("Long Prompt Test", "PASS", f"{elapsed:.2f}s, timeout_occurred=False, output_quality={quality}, raw={raw[:300]!r}")
    except Exception as exc:
        is_timeout = "timeout" in str(exc).lower() or "timed out" in str(exc).lower()
        record("Long Prompt Test", "FAIL", f"timeout_occurred={is_timeout}, error={exc}")

    # --- E. Bug #11 Simulation -------------------------------------------- #
    print("\n== E. Bug #11 Simulation ==")
    bug11_prompt = (
        "Analyze this startup idea:\n"
        "AI Voice Agent for Indian SMEs.\n\n"
        "Return structured JSON with:\n"
        "market,\n"
        "competition,\n"
        "opportunity,\n"
        "risks,\n"
        "recommendation."
    )
    try:
        start = time.perf_counter()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": bug11_prompt}],
            max_tokens=600,
            timeout=120.0,
        )
        elapsed = time.perf_counter() - start
        raw = resp.choices[0].message.content or ""
        try:
            extract_json(raw)
            record("Bug #11 Simulation", "PASS", f"{elapsed:.2f}s, structured JSON returned without timeout, raw={raw[:400]!r}")
        except Exception as parse_exc:
            record("Bug #11 Simulation", "FAIL", f"{elapsed:.2f}s, no valid JSON in response: {parse_exc}, raw={raw[:400]!r}")
    except Exception as exc:
        is_timeout = "timeout" in str(exc).lower() or "timed out" in str(exc).lower()
        record("Bug #11 Simulation", "FAIL", f"timeout_occurred={is_timeout}, error={exc}")

    print_final_report(model=model)
    return 0 if all(r["status"] == "PASS" for r in results.values()) else 1


def print_final_report(model: str | None) -> None:
    print("\n" + "=" * 72)
    print("FINAL REPORT")
    print("=" * 72)
    print(f"Model used: {model or 'N/A - connection failed'}")
    for name in ("Connection Test", "Response Time Test", "JSON Output Test", "Long Prompt Test", "Bug #11 Simulation"):
        r = results.get(name, {"status": "NOT RUN", "detail": ""})
        print(f"{name}: {r['status']}" + (f" - {r['detail']}" if r["detail"] else ""))
    all_pass = results and all(r["status"] == "PASS" for r in results.values())
    print(f"\nRecommendation: {'Suitable for AFOS' if all_pass else 'Not suitable for AFOS'}")


if __name__ == "__main__":
    sys.exit(main())
