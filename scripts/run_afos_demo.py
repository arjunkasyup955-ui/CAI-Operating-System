"""AFOS Integration Test Runner and Dashboard Preview.

Runs the complete, unmodified AFOS Phase 4 pipeline end to end for one demo
idea:

    Idea -> Founder Orchestrator -> Research Pipeline -> Decision Engine ->
    MVP Planner -> AI Builder -> Deployment Pipeline -> Growth Pipeline ->
    Founder Dashboard

Every stage below is a single, direct call into an existing, unmodified
Phase 4 component's own public run_x() function - nothing here reimplements,
wraps, or bypasses any of them. This script only adds instrumentation (timing,
status/summary/error/warning capture) and renders the final results to disk.

Outputs (outputs/):
  - founder_dashboard.json   - the raw FounderDashboard result
  - founder_dashboard.html   - a human-readable dashboard rendered from it
  - integration_report.json  - per-stage status/timing/errors/warnings + a
                                pipeline-wide PASS/FAIL verdict

Run: python scripts/run_afos_demo.py
"""

import html
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import agents.founder.ai_builder.agent as ai_builder_agent  # noqa: E402
import agents.founder.dashboard.agent as dashboard_agent  # noqa: E402
import agents.founder.decision_engine.agent as decision_engine_agent  # noqa: E402
import agents.founder.deployment_pipeline.agent as deployment_pipeline_agent  # noqa: E402
import agents.founder.growth_pipeline.agent as growth_pipeline_agent  # noqa: E402
import agents.founder.mvp_planner.agent as mvp_planner_agent  # noqa: E402
import agents.founder.orchestrator.agent as founder_orchestrator_agent  # noqa: E402
import agents.founder.research_pipeline.agent as research_pipeline_agent  # noqa: E402

OUTPUTS_DIR = REPO_ROOT / "outputs"

# Statuses that represent a genuine, graceful pipeline outcome (including a
# stage correctly declining to proceed because an earlier stage's business
# result didn't warrant it - e.g. a PIVOT/DROP recommendation, or unreachable
# infrastructure in this sandbox). Only an unhandled exception escaping a
# stage, or a stage rejecting well-formed input, counts as an integration
# FAIL - every component here already guarantees it "never raises for a
# genuine failure", so this script is verifying that contract holds end to end.
_GRACEFUL_STATUSES = {
    "completed", "approved", "researched", "planned", "built", "validated",
    "partial", "skipped", "not_recommended", "no_data", "build_failed", "healthy", "degraded", "critical",
}


def _load_demo_idea() -> dict[str, Any]:
    with open(Path(__file__).parent / "demo_idea.json", encoding="utf-8") as f:
        return json.load(f)


def _run_stage(stage_name: str, fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
    print(f"\n=== Stage: {stage_name} ===")
    start = time.monotonic()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.monotonic() - start
        status = str(result.get("status") or result.get("overall_health") or "unknown")
        warnings = result.get("warnings")
        warnings = warnings if isinstance(warnings, list) else []
        errors = [result.get("error")] if result.get("error") else []
        print(f"[{stage_name}] status='{status}' elapsed={elapsed:.2f}s")
        if errors:
            print(f"[{stage_name}] error: {errors[0]}")
        return {
            "stage": stage_name,
            "status": status,
            "passed": status in _GRACEFUL_STATUSES,
            "execution_time_seconds": round(elapsed, 3),
            "summary": result.get("summary", ""),
            "errors": errors,
            "warnings": warnings,
            "result": result,
        }
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"[{stage_name}] EXCEPTION after {elapsed:.2f}s: {exc}")
        return {
            "stage": stage_name,
            "status": "exception",
            "passed": False,
            "execution_time_seconds": round(elapsed, 3),
            "summary": "",
            "errors": [str(exc)],
            "warnings": [],
            "result": {},
        }


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _render_list(items: list[Any], empty_text: str = "None") -> str:
    if not items:
        return f"<p class='muted'>{_esc(empty_text)}</p>"
    return "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in items) + "</ul>"


def _render_kv_table(data: dict[str, Any]) -> str:
    if not data:
        return "<p class='muted'>No data available.</p>"
    rows = "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in data.items())
    return f"<table class='kv'>{rows}</table>"


def _health_badge(health: str) -> str:
    color = {"healthy": "#1a7f37", "degraded": "#9a6700", "critical": "#cf222e", "unknown": "#6e7781"}.get(health, "#6e7781")
    return f"<span class='badge' style='background:{color}'>{_esc(health.upper())}</span>"


def _render_dashboard_html(dashboard: dict[str, Any], stages: list[dict[str, Any]], demo: dict[str, Any]) -> str:
    idea = dashboard.get("idea", demo.get("idea", ""))
    overall_health = dashboard.get("overall_health", "unknown")
    decision_scores = dashboard.get("decision_scores", {})
    research_summary = dashboard.get("research_summary", {})
    market_analysis = dashboard.get("market_analysis", {})
    competitor_summary = dashboard.get("competitor_summary", {})
    mvp_plan = dashboard.get("mvp_plan", {})
    build_status = dashboard.get("build_status", {})
    deployment_status = dashboard.get("deployment_status", {})
    growth_status = dashboard.get("growth_status", {})
    alerts = dashboard.get("alerts", [])
    risks = dashboard.get("risks", [])
    next_actions = dashboard.get("recommended_next_actions", [])

    timeline_rows = "".join(
        f"<tr><td>{i + 1}</td><td>{_esc(s['stage'])}</td><td>{_esc(s['status'])}</td>"
        f"<td>{'✅' if s['passed'] else '❌'}</td><td>{s['execution_time_seconds']:.2f}s</td></tr>"
        for i, s in enumerate(stages)
    )
    total_time = sum(s["execution_time_seconds"] for s in stages)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>AFOS Founder Dashboard</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 0; background: #0d1117; color: #e6edf3; }}
  header {{ padding: 24px 32px; background: linear-gradient(135deg, #1f6feb, #388bfd); }}
  header h1 {{ margin: 0; font-size: 28px; }}
  header .tagline {{ opacity: 0.9; margin-top: 4px; }}
  main {{ padding: 24px 32px; max-width: 1100px; margin: 0 auto; }}
  section {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 18px 22px; margin-bottom: 20px; }}
  h2 {{ margin-top: 0; font-size: 18px; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
  table.kv {{ width: 100%; border-collapse: collapse; }}
  table.kv th {{ text-align: left; color: #8b949e; width: 220px; padding: 4px 8px 4px 0; vertical-align: top; }}
  table.kv td {{ padding: 4px 0; }}
  table.timeline {{ width: 100%; border-collapse: collapse; }}
  table.timeline th, table.timeline td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #30363d; }}
  ul {{ margin: 4px 0; padding-left: 20px; }}
  .muted {{ color: #8b949e; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; color: white; font-weight: 600; font-size: 13px; }}
  .idea-box {{ font-size: 16px; font-style: italic; color: #c9d1d9; }}
  .score-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }}
  .score-card {{ background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px 14px; }}
  .score-card .label {{ color: #8b949e; font-size: 12px; }}
  .score-card .value {{ font-size: 22px; font-weight: 700; }}
  footer {{ text-align: center; color: #6e7781; padding: 20px; font-size: 12px; }}
</style>
</head>
<body>
<header>
  <h1>🚀 AFOS &mdash; AI Founder Operating System</h1>
  <div class="tagline">Founder Dashboard &mdash; Idea to Launch, End to End</div>
</header>
<main>

<section>
  <h2>Idea</h2>
  <p class="idea-box">&ldquo;{_esc(idea)}&rdquo;</p>
  <table class="kv">
    <tr><th>Venture ID</th><td>{_esc(dashboard.get('venture_id', ''))}</td></tr>
    <tr><th>Overall Health</th><td>{_health_badge(overall_health)}</td></tr>
    <tr><th>Dashboard Status</th><td>{_esc(dashboard.get('status', 'unknown'))}</td></tr>
  </table>
</section>

<section>
  <h2>Research Summary</h2>
  {_render_kv_table(research_summary)}
</section>

<section>
  <h2>Market Analysis</h2>
  {_render_kv_table(market_analysis)}
</section>

<section>
  <h2>Competitors</h2>
  {_render_kv_table(competitor_summary)}
</section>

<section>
  <h2>Decision Scores</h2>
  <div class="score-grid">
    <div class="score-card"><div class="label">Opportunity Score</div><div class="value">{decision_scores.get('opportunity_score', 0)}</div></div>
    <div class="score-card"><div class="label">Risk Score</div><div class="value">{decision_scores.get('risk_score', 0)}</div></div>
    <div class="score-card"><div class="label">Overall Score</div><div class="value">{decision_scores.get('overall_score', 0)}</div></div>
    <div class="score-card"><div class="label">Recommendation</div><div class="value">{_esc(decision_scores.get('recommendation', 'unknown'))}</div></div>
  </div>
  <br/>
  {_render_kv_table(decision_scores)}
</section>

<section>
  <h2>MVP Plan</h2>
  {_render_kv_table(mvp_plan)}
</section>

<section>
  <h2>Build Status</h2>
  {_render_kv_table(build_status)}
</section>

<section>
  <h2>Deployment Status</h2>
  {_render_kv_table(deployment_status)}
</section>

<section>
  <h2>Growth Status</h2>
  {_render_kv_table(growth_status)}
</section>

<section>
  <h2>Overall Health</h2>
  <p>{_health_badge(overall_health)}</p>
  <p>{_esc(dashboard.get('summary', ''))}</p>
</section>

<section>
  <h2>Pipeline Execution Timeline</h2>
  <table class="timeline">
    <tr><th>#</th><th>Stage</th><th>Status</th><th>Passed</th><th>Time</th></tr>
    {timeline_rows}
  </table>
  <p class="muted">Total pipeline execution time: {total_time:.2f}s</p>
</section>

<section>
  <h2>Alerts</h2>
  {_render_list(alerts, "No alerts.")}
</section>

<section>
  <h2>Risks</h2>
  {_render_list(risks, "No risks identified.")}
</section>

<section>
  <h2>Recommended Next Actions</h2>
  {_render_list(next_actions, "No next actions.")}
</section>

</main>
<footer>Generated by scripts/run_afos_demo.py &mdash; AFOS Integration Test Runner and Dashboard Preview</footer>
</body>
</html>
"""


def main() -> int:
    demo = _load_demo_idea()
    idea = demo["idea"]
    venture_id = demo["venture_id"]
    research_depth = demo.get("research_depth", "standard")

    print("=" * 70)
    print("AFOS Integration Test Runner and Dashboard Preview")
    print("=" * 70)
    print(f"Idea: {idea}")
    print(f"Venture ID: {venture_id}")
    print(f"Research Depth: {research_depth}")

    stages: list[dict[str, Any]] = []
    stages.append(_run_stage("Founder Orchestrator", founder_orchestrator_agent.run_founder_pipeline, idea, venture_id))
    stages.append(_run_stage("Research Pipeline", research_pipeline_agent.run_research_pipeline, idea, venture_id, research_depth))
    stages.append(_run_stage("Decision Engine", decision_engine_agent.run_decision_engine, idea, venture_id, research_depth))
    stages.append(_run_stage("MVP Planner", mvp_planner_agent.run_mvp_planner, idea, venture_id, research_depth))
    stages.append(_run_stage("AI Builder", ai_builder_agent.run_ai_builder, idea, venture_id, research_depth))
    stages.append(_run_stage("Deployment Pipeline", deployment_pipeline_agent.run_deployment_pipeline, idea, venture_id, research_depth))
    stages.append(_run_stage("Growth Pipeline", growth_pipeline_agent.run_growth_pipeline, idea, venture_id, research_depth))
    stages.append(_run_stage("Founder Dashboard", dashboard_agent.run_founder_dashboard, idea, venture_id, research_depth))

    dashboard_result = stages[-1]["result"]

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    dashboard_json_path = OUTPUTS_DIR / "founder_dashboard.json"
    dashboard_json_path.write_text(json.dumps(dashboard_result, indent=2, default=str), encoding="utf-8")

    dashboard_html_path = OUTPUTS_DIR / "founder_dashboard.html"
    dashboard_html_path.write_text(_render_dashboard_html(dashboard_result, stages, demo), encoding="utf-8")

    overall_pass = all(s["passed"] for s in stages)
    integration_report = {
        "idea": idea,
        "venture_id": venture_id,
        "research_depth": research_depth,
        "overall_result": "PASS" if overall_pass else "FAIL",
        "components_executed": [s["stage"] for s in stages],
        "total_execution_time_seconds": round(sum(s["execution_time_seconds"] for s in stages), 3),
        "stages": [{k: v for k, v in s.items() if k != "result"} for s in stages],
    }
    integration_report_path = OUTPUTS_DIR / "integration_report.json"
    integration_report_path.write_text(json.dumps(integration_report, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 70)
    print("Integration Report Summary")
    print("=" * 70)
    for s in stages:
        mark = "PASS" if s["passed"] else "FAIL"
        print(f"  [{mark}] {s['stage']:<22} status={s['status']:<16} time={s['execution_time_seconds']:.2f}s")
    print(f"\nTotal execution time: {integration_report['total_execution_time_seconds']:.2f}s")

    print(f"\nOutputs written to: {OUTPUTS_DIR}")
    print(f"  - {dashboard_json_path.name}")
    print(f"  - {dashboard_html_path.name}")
    print(f"  - {integration_report_path.name}")

    print("\n" + ("PASS" if overall_pass else "FAIL"))

    print("\nTo view the dashboard locally, run:")
    print(f'  python -m http.server 8000 --directory "{OUTPUTS_DIR}"')
    print("  then open http://localhost:8000/founder_dashboard.html")
    print(f'\nOr open it directly:\n  start "" "{dashboard_html_path}"')

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
