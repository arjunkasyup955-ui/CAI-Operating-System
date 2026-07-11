import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tools.browser.browser  # noqa: F401  (import registers the browser_fetch tool)
import tools.web.search  # noqa: F401  (import registers the web_search tool)
from agents.infrastructure.searxng.agent import run_searxng_operation
from agents.research.competitor_intelligence.agent import competitor_intelligence_node
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.real_browser_research")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# ------------------------------------------------------------------------ #
# DI seams for the real I/O primitives this component uses. Each defaults to
# the exact same tool-registry-mediated call Phase 1's search_agent/
# browser_agent already make (never reimplementing the underlying browser or
# search provider chain) plus one independent second search source (the
# SearXNG infrastructure agent, called directly rather than through the
# tool-registry web_search chain) - this is what makes search genuinely
# multi-source rather than multi-query-against-one-chain. Swappable for
# deterministic testing via get_x_fn/set_x_fn/reset_x_fn, the same
# convention every other AFOS component uses; unlike tools/browser/
# browser_router.py and tools/web/search_router.py (which have no setter of
# their own), this component's own DI layer is fully testable without
# touching either frozen file.
# ------------------------------------------------------------------------ #

SearchFn = Callable[[str, int], list[dict[str, Any]]]
FetchFn = Callable[[str], dict[str, Any]]
SearxngFn = Callable[[str, int], dict[str, Any]]


def _default_search_fn(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    return get_tool_registry().invoke("web_search", agent_name="real_browser_research", query=query, max_results=max_results)


def _default_fetch_fn(url: str) -> dict[str, Any]:
    return get_tool_registry().invoke("browser_fetch", agent_name="real_browser_research", url=url)


def _default_searxng_fn(query: str, max_results: int = 5) -> dict[str, Any]:
    return run_searxng_operation("searxng_web_search", venture_id="real_browser_research", query=query, max_results=max_results)


_search_fn: SearchFn = _default_search_fn
_fetch_fn: FetchFn = _default_fetch_fn
_searxng_fn: SearxngFn = _default_searxng_fn


def get_search_fn() -> SearchFn:
    return _search_fn


def set_search_fn(fn: SearchFn) -> None:
    global _search_fn
    _search_fn = fn


def reset_search_fn() -> None:
    global _search_fn
    _search_fn = _default_search_fn


def get_fetch_fn() -> FetchFn:
    return _fetch_fn


def set_fetch_fn(fn: FetchFn) -> None:
    global _fetch_fn
    _fetch_fn = fn


def reset_fetch_fn() -> None:
    global _fetch_fn
    _fetch_fn = _default_fetch_fn


def get_searxng_fn() -> SearxngFn:
    return _searxng_fn


def set_searxng_fn(fn: SearxngFn) -> None:
    global _searxng_fn
    _searxng_fn = fn


def reset_searxng_fn() -> None:
    global _searxng_fn
    _searxng_fn = _default_searxng_fn


def reset_all_fns() -> None:
    reset_search_fn()
    reset_fetch_fn()
    reset_searxng_fn()


# ------------------------------------------------------------------------ #
# Automatic retry on temporary failures.
# ------------------------------------------------------------------------ #


def _with_retries(fn: Callable[[], Any], *, max_attempts: int = 3, base_delay_seconds: float = 0.15) -> Any:
    """Retries `fn` up to `max_attempts` times with a small linear backoff.
    Used around every real network/browser call in this module - a bounded,
    generic retry for transient failures (a flaky provider, a momentary
    timeout), distinct from the scheduler-level retry (see
    workflows/real_research_pipeline.py's submit_real_research_job), which
    retries the whole research run rather than one individual call.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any transient I/O failure is retried
            last_exc = exc
            logger.warning("real_browser_research: attempt %d/%d failed: %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(base_delay_seconds * attempt)
    assert last_exc is not None
    raise last_exc


# ------------------------------------------------------------------------ #
# Pure extraction / scoring logic - deterministic, no network or LLM
# dependency, so it is exhaustively unit-testable without any live service.
# ------------------------------------------------------------------------ #

_EXCLUDED_DOMAINS = {
    "wikipedia.org", "reddit.com", "youtube.com", "twitter.com", "x.com",
    "linkedin.com", "facebook.com", "medium.com", "github.com",
    "stackoverflow.com", "quora.com", "amazon.com", "google.com",
    "instagram.com", "pinterest.com", "tiktok.com",
}

_PRICING_PATTERN = re.compile(
    r"(free\s+(?:tier|plan)|freemium|\$\s?\d[\d,]*(?:\.\d+)?\s?(?:/|per\s+)?(?:mo|month|yr|year|user)?|starting\s+at\s+\$\s?\d[\d,]*)",
    re.IGNORECASE,
)

_FUNDING_PATTERN = re.compile(
    r"((?:raised|secured|closed|announced)\s+\$[\d.,]+\s?(?:million|billion|M|B)\b"
    r"|series\s+[a-e]\b"
    r"|seed\s+(?:round|funding)"
    r"|\$[\d.,]+\s?(?:million|billion|M|B)\s+in\s+funding)",
    re.IGNORECASE,
)

_WORD_PATTERN = re.compile(r"[a-z0-9]+")


def extract_domain(url: str) -> str:
    """Root domain, www-stripped, lowercased. Empty string for an unparseable URL."""
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _tokenize(text: str) -> set[str]:
    return set(_WORD_PATTERN.findall(text.lower()))


def score_relevance(idea: str, title: str, snippet: str) -> float:
    """Token-overlap relevance of a title+snippet against the venture idea, 0..1."""
    idea_tokens = _tokenize(idea)
    if not idea_tokens:
        return 0.0
    candidate_tokens = _tokenize(f"{title} {snippet}")
    if not candidate_tokens:
        return 0.0
    overlap = idea_tokens & candidate_tokens
    return round(len(overlap) / len(idea_tokens), 4)


def extract_pricing_mentions(text: str) -> list[str]:
    if not text:
        return []
    matches = [m.group(0).strip() for m in _PRICING_PATTERN.finditer(text)]
    seen: set[str] = set()
    return [m for m in matches if not (m.lower() in seen or seen.add(m.lower()))][:10]


def extract_funding_mentions(text: str) -> list[str]:
    if not text:
        return []
    matches = [m.group(0).strip() for m in _FUNDING_PATTERN.finditer(text)]
    seen: set[str] = set()
    return [m for m in matches if not (m.lower() in seen or seen.add(m.lower()))][:10]


def extract_positioning(text: str, max_len: int = 220) -> str:
    """First substantial sentence in `text` - a cheap, deterministic stand-in
    for a meta description when a real one isn't separately available.
    """
    if not text:
        return ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        cleaned = sentence.strip()
        if len(cleaned) >= 25:
            return cleaned[:max_len]
    return text.strip()[:max_len]


def compute_confidence_score(*, has_page_content: bool, num_supporting_urls: int, provider_diversity: int, relevance_score: float) -> float:
    """Weighted heuristic, clamped to [0, 1]:
    - actually having fetched real page content (not just a search snippet)
      is the strongest signal
    - being surfaced by more than one distinct query/provider corroborates it
    - raw token-overlap relevance to the idea contributes the rest
    """
    score = 0.0
    score += 0.4 if has_page_content else 0.0
    score += min(num_supporting_urls, 4) * 0.08
    score += min(provider_diversity, 2) * 0.06
    score += min(relevance_score, 1.0) * 0.2
    return round(max(0.0, min(1.0, score)), 4)


def _is_excluded_domain(domain: str) -> bool:
    return any(domain == excluded or domain.endswith(f".{excluded}") for excluded in _EXCLUDED_DOMAINS)


def dedupe_by_key(items: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        key = key_fn(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def discover_competitors(search_results: list[dict[str, Any]], idea: str, max_competitors: int = 8) -> list[dict[str, Any]]:
    """Groups deduplicated search results by root domain (excluding generic
    non-competitor domains), scores each candidate by average relevance and
    how many distinct queries/providers surfaced it, and returns the top
    `max_competitors` candidates ranked by that score. Purely deterministic -
    no LLM call, and no network call of its own (operates on results already
    fetched by multi_source_search).
    """
    by_domain: dict[str, dict[str, Any]] = {}
    for result in search_results:
        domain = extract_domain(result.get("url", ""))
        if not domain or _is_excluded_domain(domain):
            continue
        relevance = score_relevance(idea, result.get("title", ""), result.get("snippet", ""))
        bucket = by_domain.setdefault(domain, {
            "name": (result.get("title", "") or domain).split(" - ")[0].split(" | ")[0].strip() or domain,
            "supporting_urls": [],
            "snippets": [],
            "providers": set(),
            "relevance_scores": [],
        })
        bucket["supporting_urls"].append(result.get("url", ""))
        if result.get("snippet"):
            bucket["snippets"].append(result["snippet"])
        bucket["providers"].add(result.get("source_provider") or result.get("provider") or "unknown")
        bucket["relevance_scores"].append(relevance)

    candidates = []
    for domain, bucket in by_domain.items():
        avg_relevance = sum(bucket["relevance_scores"]) / len(bucket["relevance_scores"]) if bucket["relevance_scores"] else 0.0
        candidates.append({
            "name": bucket["name"],
            "website": domain,
            "supporting_urls": list(dict.fromkeys(bucket["supporting_urls"])),
            "snippets": bucket["snippets"],
            "provider_diversity": len(bucket["providers"]),
            "candidate_score": round(avg_relevance, 4),
        })
    candidates.sort(key=lambda c: (c["candidate_score"], c["provider_diversity"]), reverse=True)
    return candidates[:max_competitors]


def enrich_competitor(competitor: dict[str, Any]) -> dict[str, Any]:
    """Fetches a competitor's homepage via the real browser fetch DI seam to
    extract pricing/funding/positioning signals from real page content. Never
    raises: a completely unreachable site still produces a valid, lower-
    confidence comparison-table row built from its search snippets alone
    (graceful fallback if a source fails).
    """
    website = competitor.get("website", "")
    content = ""
    fetch_status = "not_attempted"
    if website:
        try:
            fetch_result = _with_retries(lambda: get_fetch_fn()(f"https://{website}"))
            content = fetch_result.get("content", "") or ""
            fetch_status = "ok"
        except Exception as exc:
            logger.warning("real_browser_research: page fetch failed for %s, falling back to search snippets: %s", website, exc)
            fetch_status = f"failed: {exc}"

    extraction_text = content or " ".join(competitor.get("snippets", []))
    pricing = extract_pricing_mentions(extraction_text)
    funding = extract_funding_mentions(extraction_text)
    positioning = extract_positioning(extraction_text)
    confidence = compute_confidence_score(
        has_page_content=bool(content),
        num_supporting_urls=len(competitor.get("supporting_urls", [])),
        provider_diversity=competitor.get("provider_diversity", 1),
        relevance_score=competitor.get("candidate_score", 0.0),
    )
    return {
        **competitor,
        "content": content,
        "pricing": pricing,
        "funding": funding,
        "positioning": positioning,
        "confidence_score": confidence,
        "fetch_status": fetch_status,
        "citations": list(dict.fromkeys(competitor.get("supporting_urls", []))),
    }


def build_comparison_table(enriched_competitors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uniform, JSON-ready rows: one per competitor, sorted by confidence
    descending. This is the literal competitor comparison table.
    """
    table = [
        {
            "name": c.get("name", c.get("website", "")),
            "website": c.get("website", ""),
            "pricing": c.get("pricing", []),
            "funding": c.get("funding", []),
            "positioning": c.get("positioning", ""),
            "confidence_score": c.get("confidence_score", 0.0),
            "citations": c.get("citations", []),
            "fetch_status": c.get("fetch_status", "not_attempted"),
        }
        for c in enriched_competitors
    ]
    table.sort(key=lambda row: row["confidence_score"], reverse=True)
    return table


def multi_source_search(idea: str, extra_queries: list[str] | None = None) -> dict[str, Any]:
    """Runs the idea plus a few derived queries through two independent real
    search sources - the tool-registry web_search tool (itself a fallback
    chain across searxng/tavily/brave) and the SearXNG infrastructure agent
    called directly - merging and deduplicating results by URL. Each
    (source, query) pair's failure is caught independently; the pipeline
    continues with whatever combination succeeded, and every outcome (success
    or failure) is recorded in sources_status for observability.
    """
    queries = [idea, *(extra_queries if extra_queries is not None else [f"{idea} competitors", f"{idea} pricing", f"{idea} funding"])]
    all_results: list[dict[str, Any]] = []
    sources_status: dict[str, str] = {}

    for query in queries:
        try:
            results = _with_retries(lambda q=query: get_search_fn()(q, 5))
            for raw in results:
                entry = dict(raw)
                entry["source_provider"] = entry.get("provider") or "web_search"
                entry["query"] = query
                all_results.append(entry)
            sources_status[f"web_search::{query}"] = "ok"
        except Exception as exc:
            logger.warning("real_browser_research: web_search source failed for query %r: %s", query, exc)
            sources_status[f"web_search::{query}"] = f"failed: {exc}"

        try:
            searxng_result = _with_retries(lambda q=query: get_searxng_fn()(q, 5))
            if searxng_result.get("success"):
                for raw in searxng_result.get("results", []):
                    all_results.append({
                        "title": raw.get("title", ""),
                        "url": raw.get("url", ""),
                        "snippet": raw.get("content", ""),
                        "source_provider": "searxng_direct",
                        "query": query,
                    })
                sources_status[f"searxng_direct::{query}"] = "ok"
            else:
                reason = searxng_result.get("error") or searxng_result.get("reason") or "unknown"
                sources_status[f"searxng_direct::{query}"] = f"failed: {reason}"
        except Exception as exc:
            logger.warning("real_browser_research: searxng_direct source failed for query %r: %s", query, exc)
            sources_status[f"searxng_direct::{query}"] = f"failed: {exc}"

    deduped = dedupe_by_key(all_results, key_fn=lambda r: r.get("url", ""))
    logger.info("real_browser_research: multi_source_search collected %d unique results across %d queries", len(deduped), len(queries))
    return {"results": deduped, "queries": queries, "sources_status": sources_status}


def real_browser_node(state: VentureState) -> dict[str, Any]:
    """Drop-in replacement for the Research Pipeline's 'browser' stage slot
    (see workflows/real_research_pipeline.py's set_stage_functions call) that
    performs genuine multi-source search plus competitor discovery and
    per-competitor page enrichment, instead of a single search-result URL
    fetch.

    Tags its primary finding with agent='browser_agent' and, on success, the
    exact original event name 'browser_fetch_completed' - not because this is
    the original browser_agent, but because workflows/research_pipeline.py's
    report_node and agents/research/competitor_intelligence/agent.py's
    _gather_context() both key strictly off that fixed (agent, event) pair
    for the 'browser' stage slot, by design (the same convention every fake
    stage function in scripts/smoke_test_phase4_research_pipeline.py already
    follows). This is what lets the frozen, unmodified Market Intelligence and
    Competitor Intelligence agents automatically consume this component's
    real content with zero changes to either file.
    """
    idea = state.get("idea", "")
    venture_id = state.get("venture_id", "default")
    logger.info("real_browser_research: starting multi-source research for venture_id=%s idea=%r", venture_id, idea)

    try:
        search_bundle = multi_source_search(idea)
        results = search_bundle["results"]
        if not results:
            logger.warning("real_browser_research: no search results from any source for idea=%r", idea)
            entry = {
                "agent": "browser_agent",
                "event": "real_browser_failed",
                "reason": "no search results from any configured source",
                "sources_status": search_bundle["sources_status"],
            }
            return {"research_findings": [entry], "history": [entry]}

        candidates = discover_competitors(results, idea)
        if not candidates:
            logger.warning("real_browser_research: search succeeded but no competitor domains were discoverable")
            entry = {
                "agent": "browser_agent",
                "event": "real_browser_failed",
                "reason": "no competitor domains discoverable from search results",
                "search_result_count": len(results),
                "sources_status": search_bundle["sources_status"],
            }
            return {"research_findings": [entry], "history": [entry]}

        enriched = [enrich_competitor(c) for c in candidates]
        enriched.sort(key=lambda c: c["confidence_score"], reverse=True)
        table = build_comparison_table(enriched)
        citations = sorted({url for c in enriched for url in c.get("citations", [])})

        primary = enriched[0]
        entry = {
            "agent": "browser_agent",
            "event": "browser_fetch_completed",
            "url": f"https://{primary['website']}" if primary.get("website") else "",
            "content": primary.get("content", "")[:5000],
            "content_length": len(primary.get("content", "")),
            "search_result_count": len(results),
            "competitor_count": len(enriched),
            "competitor_comparison_table": table,
            "sources_status": search_bundle["sources_status"],
            "citations": citations,
            "real_research": True,
        }
        logger.info("real_browser_research: completed for venture_id=%s with %d competitors discovered", venture_id, len(enriched))
        return {"research_findings": [entry], "history": [entry]}
    except Exception as exc:
        logger.error("real_browser_research: unexpected failure for venture_id=%s: %s", venture_id, exc)
        entry = {"agent": "browser_agent", "event": "real_browser_failed", "error": str(exc)}
        return {"research_findings": [entry], "history": [entry]}


def real_competitor_intelligence_node(state: VentureState) -> dict[str, Any]:
    """Drop-in replacement for the Research Pipeline's 'competitor_intelligence'
    stage slot. Runs the frozen Phase 1 competitor_intelligence_node
    unmodified first (real LLM narrative synthesis when a model is
    configured, or a graceful skip/failure when it isn't), then layers this
    component's own deterministic, structured competitor_comparison_table on
    top - so the report's competitor_analysis field always reflects real,
    structured discovery, even in an environment with no LLM configured at
    all.
    """
    base_result = competitor_intelligence_node(state)
    base_findings = list(base_result.get("research_findings", []))
    base_history = list(base_result.get("history", []))

    table: list[dict[str, Any]] = []
    citations: list[str] = []
    sources_status: dict[str, str] = {}
    for finding in state.get("research_findings", []):
        if finding.get("agent") == "browser_agent" and finding.get("real_research"):
            table = finding.get("competitor_comparison_table", [])
            citations = finding.get("citations", [])
            sources_status = finding.get("sources_status", {})

    if not table:
        return base_result

    last_base = base_findings[-1] if base_findings else {}
    narrative_fields = (
        {k: v for k, v in last_base.items() if k not in ("agent", "event", "reason", "error")}
        if last_base.get("event") == "competitor_analysis_completed"
        else {}
    )

    merged_entry = {
        "agent": "competitor_intelligence_agent",
        "event": "real_competitor_comparison_completed",
        **narrative_fields,
        "competitor_comparison_table": table,
        "citations": citations,
        "sources_status": sources_status,
        "competitor_count": len(table),
        "real_research": True,
    }
    logger.info("real_browser_research: real_competitor_intelligence merged narrative + structured table (%d competitors)", len(table))
    return {
        "research_findings": [*base_findings, merged_entry],
        "history": [*base_history, {"agent": "competitor_intelligence_agent", "action": "real_competitor_comparison_completed"}],
    }
