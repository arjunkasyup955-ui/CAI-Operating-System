import csv
import io
import json
from datetime import datetime, timezone
from typing import Any

from backend.dashboard.pdf_export import generate_pdf_report

CSV_FIELDNAMES = ("execution_id", "pipeline_name", "venture_id", "status", "execution_time_seconds", "confidence_score", "competitor_count", "recorded_at")

SUPPORTED_FORMATS = ("json", "csv", "html", "pdf")


def export_json(metrics: dict[str, Any], health: dict[str, Any], history: list[dict[str, Any]]) -> bytes:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "health": health,
        "history": history,
    }
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def export_csv(history: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_FIELDNAMES), extrasaction="ignore")
    writer.writeheader()
    for entry in history:
        writer.writerow(entry)
    return buf.getvalue().encode("utf-8")


def _escape_html(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def export_html(metrics: dict[str, Any], health: dict[str, Any], history: list[dict[str, Any]]) -> bytes:
    rows = "".join(
        "<tr>"
        f"<td>{_escape_html(e['execution_id'][:8])}</td>"
        f"<td>{_escape_html(e['pipeline_name'])}</td>"
        f"<td>{_escape_html(e['venture_id'])}</td>"
        f"<td>{_escape_html(e['status'])}</td>"
        f"<td>{_escape_html(e.get('execution_time_seconds', ''))}</td>"
        f"<td>{_escape_html(e.get('confidence_score', ''))}</td>"
        f"<td>{_escape_html(e.get('competitor_count', ''))}</td>"
        "</tr>"
        for e in history
    )
    execution = metrics.get("execution", {})
    cache = metrics.get("cache", {})
    scheduler = metrics.get("scheduler", {})
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AFOS Dashboard Report</title>
<style>
body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #0f1117; color: #e5e7eb; padding: 24px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
th, td {{ border: 1px solid #2a2e3a; padding: 8px; text-align: left; }}
th {{ background: #1a1d29; }}
h1, h2 {{ color: #60a5fa; }}
.metric {{ display: inline-block; margin: 8px 16px 8px 0; }}
</style></head>
<body>
<h1>AFOS Founder Dashboard Report</h1>
<h2>Health: {_escape_html(health.get('overall_health', 'unknown'))} (score {_escape_html(health.get('overall_score', 0))})</h2>
<div>
<span class="metric">Total Executions: {_escape_html(execution.get('total_executions', 0))}</span>
<span class="metric">Cache Hit Rate: {_escape_html(cache.get('hit_rate', 0))}</span>
<span class="metric">Total Jobs: {_escape_html(scheduler.get('total_jobs', 0))}</span>
</div>
<h2>Execution History</h2>
<table>
<tr><th>ID</th><th>Pipeline</th><th>Venture</th><th>Status</th><th>Time (s)</th><th>Confidence</th><th>Competitors</th></tr>
{rows}
</table>
</body></html>"""
    return html.encode("utf-8")


def export_pdf(metrics: dict[str, Any], health: dict[str, Any], history: list[dict[str, Any]]) -> bytes:
    execution = metrics.get("execution", {})
    cache = metrics.get("cache", {})
    scheduler = metrics.get("scheduler", {})
    lines = [
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Overall Health: {health.get('overall_health', 'unknown')} (score {health.get('overall_score', 0)})",
        f"Pipeline Health: {health.get('pipeline_health', 'unknown')}",
        f"Scheduler Health: {health.get('scheduler_health', 'unknown')}",
        f"Research Health: {health.get('research_health', 'unknown')}",
        f"Builder Health: {health.get('builder_health', 'unknown')}",
        f"Deployment Health: {health.get('deployment_health', 'unknown')}",
        "",
        f"Total Executions: {execution.get('total_executions', 0)}",
        f"Completed: {execution.get('completed', 0)}  Failed: {execution.get('failed', 0)}  Running: {execution.get('running', 0)}",
        f"Average Execution Time: {execution.get('average_execution_time_seconds', 0)}s",
        f"Average Confidence Score: {execution.get('average_confidence_score', 0)}",
        f"Cache Hit Rate: {cache.get('hit_rate', 0)}  (hits={cache.get('hits', 0)} misses={cache.get('misses', 0)})",
        f"Scheduler Total Jobs: {scheduler.get('total_jobs', 0)}",
        "",
        "Execution History:",
    ]
    for entry in history:
        lines.append(
            f"  {entry['execution_id'][:8]}  {entry['pipeline_name']}  {entry['venture_id']}  "
            f"{entry['status']}  {entry.get('execution_time_seconds', '')}s  conf={entry.get('confidence_score', '')}"
        )
    return generate_pdf_report("AFOS Founder Dashboard Report", lines)


def export(fmt: str, metrics: dict[str, Any], health: dict[str, Any], history: list[dict[str, Any]]) -> tuple[bytes, str]:
    if fmt == "json":
        return export_json(metrics, health, history), "application/json"
    if fmt == "csv":
        return export_csv(history), "text/csv"
    if fmt == "html":
        return export_html(metrics, health, history), "text/html"
    if fmt == "pdf":
        return export_pdf(metrics, health, history), "application/pdf"
    raise ValueError(f"unsupported export format: {fmt!r} (supported: {SUPPORTED_FORMATS})")
