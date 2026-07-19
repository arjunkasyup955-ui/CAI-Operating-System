# AFOS Bug Investigation Log

Permanent record of the research-pipeline / scoring-formula investigation on
`phase-1`. Moved here from a session scratchpad once the investigation closed.

## Bug list

- **Bug #10** (research cascade-skip, no browser provider) — RESOLVED. No
  browser-fetch provider was installed/configured (Playwright/Crawl4AI missing,
  Firecrawl unconfigured), so `browser_agent` always failed and all 5
  downstream analysis stages cascade-skipped with empty content, producing
  identical neutral-default scores for every idea. Fixed by installing
  Playwright + Chromium.

- **Bug #11** (score clustering) — RESOLVED. Root cause: a hidden outer 30s
  timeout in `workflows/founder_pipeline.py` (`_RESEARCH_TIMEOUT_SECONDS`)
  wrapped the *entire* 8-worker research graph invocation as one unit, killing
  it before any real LLM call could complete — raised to 300.0. Also traced
  and fixed a 7x redundant research re-invocation per idea (dashboard's 7
  stages each independently re-derived research from scratch) via the existing
  `set_research_pipeline_invoker`/`reset_research_pipeline_invoker` DI seam in
  `workflows/decision_engine.py`, cutting real LLM call volume ~69%.

- **Bug #12** (Nemotron occasionally returns a JSON field as a list instead of
  a string, a Pydantic validation error despite `json_mode=True`) — **OPEN,
  not fixed**, low severity, explicitly deferred by user direction.

- **Bug #13** (silent fallback to neutral-default scores under provider
  failure, no visible indicator) — RESOLVED. Added a `research_data_quality`
  flag (`genuine`/`partial_fallback`/`full_fallback`) computed in
  `workflows/founder_dashboard.py` from stage-level ok/fail counts, surfaced
  through `agents/founder/dashboard/agent.py`'s `FounderDashboard` Pydantic
  contract (had to be added there too — Pydantic's default `extra="ignore"`
  was silently stripping the field before the fix) and into
  `scripts/production_validation.py`'s scorecard output.

- **Bug #14** (scoring-formula scale mismatch) — RESOLVED. All 17 ideas tested
  after Bug #11/#13 landed on PIVOT, none reaching VALIDATE FIRST (>=6.0),
  with the highest score only 0.04 below the threshold. Root cause found via a
  direct raw-research probe (3 ideas, zero-LLM-cost via git-stash A/B
  comparison): `agents/research/idea_validation/agent.py` prompts Nemotron for
  `competition_score`/`timing_score`/`execution_score` on a 0.0-1.0 scale
  (explicit in the prompt). `workflows/decision_engine.py` consumed
  `validation_competition_score`/`validation_execution_score`/
  `validation_timing_score` **without** the `x10.0` normalization applied to
  sibling 0-1-scale fields elsewhere in the same function
  (`opportunity_priority_score`, `trend_score`). Fixed with a 4-line diff
  (commit `046b9cd`) applying `* 10.0` to the three affected reads. Verified
  on 3 probe ideas before the full re-run: `overall_score` moved from
  PIVOT-band (5.27-5.44) to VALIDATE-FIRST-band (7.03-7.37) for identical
  underlying research data.

## Bug #14 fix — full 25-idea re-run confirmation

All 25 ideas in `ideas.csv` were re-tested in disciplined batches
(`ideas_batch1.csv` through `ideas_batch6.csv`) after the fix landed.

**Recommendation bands (25 total):**

| Band | Count |
|---|---|
| BUILD NOW (>=8.0) | 0 |
| VALIDATE FIRST (6.0-7.99) | 22 |
| PIVOT (4.0-5.99) | 3 |
| DROP (<4.0) | 0 |

Before the fix: 0/25 reached VALIDATE FIRST. After: 22/25.

**`research_data_quality` (Bug #13's flag, validated in production use):**

| Quality | Count |
|---|---|
| genuine | 23 |
| partial_fallback | 2 |
| full_fallback | 0 |

The 2 `partial_fallback` ideas (adaptive math tutoring, energy optimization
SaaS) are 2 of the 3 PIVOT ideas — correctly caught by the flag rather than
silently trusted; their root causes (a research-stage failure and a
site-specific browser-fetch failure respectively) are unrelated to the
scoring fix. The third PIVOT idea (API documentation generator, 5.99) has
genuine data — a legitimate near-miss of the threshold.

**Reliability:** zero unexplained errors or crashes across all 5 re-run
batches — 25/25 checks passed, 100% health score every run. Every failure
observed (NIM 429s, missing local Postgres/n8n/searxng, one browser
protocol error) degraded gracefully per the established "Provider
Unavailable, not Code Failure" pattern.

## Infrastructure gaps found and addressed

Once ideas started reaching VALIDATE FIRST, the dashboard's MVP/Build/
Deployment stages executed for the first time in this investigation
(previously always skipped, since every idea was `not_recommended`),
surfacing pre-existing local infra gaps in `workflows/deployment_pipeline.py`'s
5-stage prep (docker/postgres/chroma/ollama/n8n):

- `chromadb` and `docker` (docker-py SDK) — pip packages, were missing,
  installed. Both fully functional after install (chromadb is embedded/
  file-based, no server needed; a Docker daemon was already reachable on this
  machine).
- `psycopg[binary,pool]` — pip package, deliberately left uninstalled; even
  installed, still needs an actual Postgres **server**, which isn't running.
- n8n, searxng — no missing pip package (both use `httpx` against a local
  HTTP endpoint); both are simply not running as local services.

Deployment stage `ready_stage_count` improved 1/5 -> 3/5 (docker + chroma now
prepared, alongside ollama which already worked) via a zero-LLM-cost probe
using the existing `set_ai_builder_invoker` DI seam in
`workflows/deployment_pipeline.py`. Postgres/n8n remain down by choice —
standing up real services was explicitly out of scope for this investigation.

## Known process risk (still open)

The AI Builder's own commit step (`agents/coding/git/agent.py`) does a
`git add .` sweep before committing its scaffold, which has repeatedly swept
unrelated working-tree files (test fixture CSVs, `.pid` files, a chroma
binary) into unrelated "AI Builder: initial scaffold for X" commits — most
recently 17 times in a row during the Bug #14 batch re-runs, once every idea
started reaching VALIDATE FIRST and triggering real AI Builder execution.
Flagged for future work; not fixed here (out of scope, and the resulting
noise commits were local-only and squashed out before push each time it
mattered).
