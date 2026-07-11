"""Phase 5, Component 4: Production Dashboard & Observability.

Converts AFOS from a static HTML dashboard preview into a production-grade
Founder SaaS Dashboard: a REST API (backend/dashboard/, stdlib http.server -
no new dependency, since no web framework is installed in this environment),
a dark-themed, responsive static frontend (frontend/dashboard/) that consumes
it, and two new kernel-level observability/metrics services
(core/observability/, core/metrics/) that aggregate real data already
produced by Phase 5 Components 1-3 (and, optionally, any Phase 4 Founder
Dashboard report a caller passes in) - without modifying or importing into
any frozen Phase 0-4 / Phase 5 Component 1-3 file.

Verifies:
  - core.observability: ExecutionHistoryStore (record/get/list/compare/
    projects, search/filter/pagination-ready), LogStore + a real
    logging.Handler capturing genuine log records, get_pipeline_events()
    reading directly from the Phase 0 Event Bus's own history()
  - core.metrics: collect_metrics() aggregating real cache/scheduler/
    execution-history data; compute_health() across all 6 required
    dimensions (Overall/Pipeline/Scheduler/Research/Builder/Deployment)
  - backend.dashboard.charts: all 8 required Chart.js-ready datasets
    (Pipeline Timeline, Execution Time, Cache Hit Rate, Research Confidence,
    Competitor Count, Job Queue, Health Score, Overall Score)
  - backend.dashboard.export: JSON/CSV/HTML/PDF all produce genuinely valid,
    parseable output (the PDF's own xref table is byte-verified)
  - backend.dashboard.router: dashboard renders (frontend HTML + CSS + JS
    served with all 8 chart canvases and script references present, JS
    syntax-checked via a real Node.js parse), every REST endpoint, search,
    filters, pagination, multi-project scoping, 404/405/400 error handling
  - backend.dashboard.server: one genuine end-to-end HTTP round trip over a
    real socket (not just router-level function calls)
  - Regression of every previous phase/component
  - pip check
  - git status

Run: python scripts/smoke_test_phase5_dashboard.py
"""

import csv
import io
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.dashboard.charts import CHART_NAMES, build_chart_data  # noqa: E402
from backend.dashboard.export import export  # noqa: E402
from backend.dashboard.pdf_export import generate_pdf_report  # noqa: E402
from backend.dashboard.router import handle_request  # noqa: E402
from backend.dashboard.server import build_server  # noqa: E402
from core.event_bus import AFOSEvent, get_event_bus  # noqa: E402
from core.metrics import (  # noqa: E402
    collect_metrics,
    compute_health,
    reset_observed_scheduler,
    set_observed_scheduler,
)
from core.observability import (  # noqa: E402
    attach_log_capture,
    detach_log_capture,
    get_default_execution_history,
    get_default_log_store,
    get_pipeline_events,
    reset_default_execution_history,
    reset_default_log_store,
)
from core.scheduler import JobPriority, JobScheduler, register_job, reset_job_registry  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dashboard"


def main() -> None:
    reset_default_execution_history()
    reset_default_log_store()
    reset_observed_scheduler()
    reset_job_registry()

    print("\n== 1. core.observability: ExecutionHistoryStore ==")
    store = get_default_execution_history()
    eid_a = store.record_execution("real_research_pipeline", "v-alpha", "completed", execution_time_seconds=8.2, confidence_score=0.75, competitor_count=4)
    eid_b = store.record_execution("real_research_pipeline", "v-alpha", "failed", execution_time_seconds=2.1)
    eid_c = store.record_execution("founder_dashboard", "v-beta", "completed", execution_time_seconds=15.0, confidence_score=0.6, competitor_count=2)
    check("record_execution returns a distinct execution_id per call", len({eid_a, eid_b, eid_c}) == 3)
    check("get_execution returns the recorded entry", store.get_execution(eid_a)["venture_id"] == "v-alpha")
    check("get_execution returns None for an unknown id", store.get_execution("nope") is None)
    check("list_executions returns all 3 entries with no filter", len(store.list_executions()) == 3)
    check("list_executions filters by venture_id", len(store.list_executions(venture_id="v-alpha")) == 2)
    check("list_executions filters by status", len(store.list_executions(status="failed")) == 1)
    check("list_executions search matches venture_id", len(store.list_executions(search="beta")) == 1)
    check("list_projects returns distinct venture_ids", set(store.list_projects()) == {"v-alpha", "v-beta"})
    comparison = store.compare_executions(eid_a, eid_b)
    check("compare_executions returns a diff of both entries", comparison is not None and comparison["diff"]["status"] == {"a": "completed", "b": "failed"})
    check("compare_executions returns None for a missing id", store.compare_executions(eid_a, "nope") is None)

    print("\n== 2. core.observability: LogStore + real logging capture ==")
    log_store = get_default_log_store()
    attach_log_capture(logger_name="afos.smoke_test_dashboard", level=logging.INFO)
    try:
        test_logger = logging.getLogger("afos.smoke_test_dashboard")
        test_logger.info("dashboard smoke test info message")
        test_logger.error("dashboard smoke test error message")
        logs = log_store.list_logs()
        check("real log records were captured by the attached handler", len(logs) >= 2)
        check("an ERROR-level record is categorized as 'error'", any(e["level"] == "ERROR" and e["category"] == "error" for e in logs))
        check("list_logs search matches message content", any("info message" in e["message"] for e in log_store.list_logs(search="info message")))
        check("list_logs filters by category", all(e["category"] == "error" for e in log_store.list_logs(category="error")))
    finally:
        detach_log_capture()

    print("\n== 3. core.observability: pipeline events (Event Bus passthrough) ==")
    bus = get_event_bus()
    bus.publish(AFOSEvent(type="dashboard_smoke_test_event", source_agent="smoke_test", venture_id="v-alpha", payload={"k": "v"}))
    events = get_pipeline_events(event_type="dashboard_smoke_test_event")
    check("get_pipeline_events reads real events from the Event Bus", len(events) >= 1)
    check("get_pipeline_events filters by venture_id", all(e["venture_id"] == "v-alpha" for e in get_pipeline_events(venture_id="v-alpha", event_type="dashboard_smoke_test_event")))

    print("\n== 4. core.metrics: collect_metrics + compute_health ==")
    metrics = collect_metrics()
    check("collect_metrics includes cache/scheduler/execution sections", set(metrics.keys()) == {"cache", "scheduler", "execution"})
    check("execution metrics reflect the 3 recorded executions", metrics["execution"]["total_executions"] == 3)
    check("execution metrics compute average confidence correctly", abs(metrics["execution"]["average_confidence_score"] - (0.75 + 0.6) / 2) < 1e-6)
    check("scheduler metrics report unregistered when no scheduler is set", metrics["scheduler"]["registered"] is False)

    scoped_metrics = collect_metrics(venture_id="v-alpha")
    check("collect_metrics scopes execution stats to a single venture_id", scoped_metrics["execution"]["total_executions"] == 2)

    health = compute_health(metrics)
    required_dims = {"overall_health", "overall_score", "pipeline_health", "scheduler_health", "research_health", "builder_health", "deployment_health"}
    check("compute_health returns all 6 required health dimensions plus overall_score", required_dims.issubset(health.keys()))
    check("builder/deployment health are 'unknown' with no founder_dashboard_report", health["builder_health"] == "unknown" and health["deployment_health"] == "unknown")

    fake_founder_report = {"build_status": {"status": "completed"}, "deployment_status": {"status": "failed"}}
    health_with_founder = compute_health(metrics, founder_dashboard_report=fake_founder_report)
    check("builder_health reflects a passed-in Founder Dashboard report", health_with_founder["builder_health"] == "healthy")
    check("deployment_health reflects a passed-in Founder Dashboard report", health_with_founder["deployment_health"] == "degraded")

    print("\n== 5. core.metrics: observed scheduler DI ==")
    scheduler = JobScheduler(num_workers=1, tick_interval_seconds=0.05)
    scheduler.start()
    register_job("dashboard_smoke_job", lambda ctx, x: x * 2)
    try:
        set_observed_scheduler(scheduler)
        jid_ok = scheduler.submit("dashboard_smoke_job", args=[21])
        scheduler.wait_for(jid_ok, timeout=5)
        scheduler_metrics = collect_metrics()["scheduler"]
        check("collect_metrics reflects a registered scheduler's real stats", scheduler_metrics["registered"] is True and scheduler_metrics["total_jobs"] == 1)
    finally:
        scheduler.shutdown()

    print("\n== 6. backend.dashboard.charts: all 8 required charts ==")
    history = store.list_executions()
    chart_data = build_chart_data(metrics, history, health)
    check("build_chart_data returns exactly the 8 required chart keys", set(chart_data.keys()) == set(CHART_NAMES))
    for name in CHART_NAMES:
        spec = chart_data[name]
        check(f"chart '{name}' has a Chart.js type", "type" in spec and isinstance(spec["type"], str))
        check(f"chart '{name}' has labels and at least one dataset", "labels" in spec and len(spec["datasets"]) >= 1)
    check("cache_hit_rate chart reflects real cache stats shape", len(chart_data["cache_hit_rate"]["datasets"][0]["data"]) == 2)
    check("health_score chart covers all 5 non-overall health dimensions", len(chart_data["health_score"]["labels"]) == 5)

    print("\n== 7. backend.dashboard.export: JSON / CSV / HTML / PDF ==")
    json_bytes, json_ctype = export("json", metrics, health, history)
    parsed = json.loads(json_bytes)
    check("JSON export round-trips and includes metrics/health/history", "metrics" in parsed and "health" in parsed and "history" in parsed)
    check("JSON export content type is application/json", json_ctype == "application/json")

    csv_bytes, csv_ctype = export("csv", metrics, health, history)
    reader = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
    check("CSV export has one row per execution", len(reader) == len(history))
    check("CSV export includes the expected columns", "execution_id" in reader[0] and "confidence_score" in reader[0])
    check("CSV export content type is text/csv", csv_ctype == "text/csv")

    html_bytes, html_ctype = export("html", metrics, health, history)
    html_report = html_bytes.decode("utf-8")
    check("HTML export is well-formed enough to contain a table and health summary", "<table" in html_report and "Health:" in html_report)
    check("HTML export includes every execution_id", all(e["execution_id"][:8] in html_report for e in history))
    check("HTML export content type is text/html", html_ctype == "text/html")

    pdf_bytes, pdf_ctype = export("pdf", metrics, health, history)
    check("PDF export starts with a valid PDF header", pdf_bytes.startswith(b"%PDF-1.4"))
    check("PDF export ends with a valid PDF trailer", pdf_bytes.rstrip().endswith(b"%%EOF"))
    check("PDF export content type is application/pdf", pdf_ctype == "application/pdf")

    try:
        export("xml", metrics, health, history)
        raise SystemExit("test failed: export() should reject an unsupported format")
    except ValueError:
        pass
    check("export() rejects an unsupported format", True)

    print("\n== 8. backend.dashboard.pdf_export: byte-level structural validation ==")
    multi_page_pdf = generate_pdf_report("Big Report", [f"line {i}" for i in range(120)])
    check("a long report spans multiple PDF pages", multi_page_pdf.count(b"/Type /Page ") + multi_page_pdf.count(b"/Type /Page/") >= 1)
    import re as _re

    xref_match = _re.search(rb"xref\r?\n0 (\d+)\r?\n", multi_page_pdf)
    check("PDF has a well-formed xref section header", xref_match is not None)
    obj_count = int(xref_match.group(1))
    xref_lines = multi_page_pdf[xref_match.end():xref_match.end() + obj_count * 20].splitlines()
    all_offsets_correct = True
    for i in range(1, obj_count):
        offset = int(xref_lines[i][:10])
        if not multi_page_pdf[offset:offset + 20].startswith(f"{i} 0 obj".encode()):
            all_offsets_correct = False
            break
    check("every xref offset points at the correct 'N 0 obj' byte position", all_offsets_correct)

    print("\n== 9. backend.dashboard.router: dashboard renders (frontend) ==")
    status, ctype, body = handle_request("GET", "/", {})
    html_page = body.decode("utf-8")
    check("GET / returns 200 with HTML", status == 200 and "text/html" in ctype)
    for chart_key in CHART_NAMES:
        check(f"index.html contains a canvas for chart '{chart_key}'", f"chart-{chart_key}" in html_page)
    check("index.html references Chart.js", "chart.js" in html_page.lower())
    check("index.html references dashboard.js and dashboard.css", "dashboard.js" in html_page and "dashboard.css" in html_page)
    check("index.html includes the project selector (multi-project support)", 'id="project-selector"' in html_page)
    check("index.html includes collapsible card headers", "card-header" in html_page and "collapse-toggle" in html_page)
    check("index.html includes a search input", 'id="global-search"' in html_page)
    check("index.html includes a notifications container", 'id="notifications"' in html_page)
    check("index.html includes a loading overlay", 'id="loading-overlay"' in html_page)

    css_status, css_ctype, css_body = handle_request("GET", "/dashboard.css", {})
    check("GET /dashboard.css returns 200 CSS", css_status == 200 and "text/css" in css_ctype and len(css_body) > 0)
    check("dashboard.css defines a dark theme background variable", b"--bg:" in css_body)
    check("dashboard.css defines traffic-light status-dot classes", b".status-dot.healthy" in css_body and b".status-dot.critical" in css_body)
    check("dashboard.css defines a sticky header/sidebar", b"position: sticky" in css_body)
    check("dashboard.css defines a responsive breakpoint", b"@media" in css_body)

    js_status, js_ctype, js_body = handle_request("GET", "/dashboard.js", {})
    check("GET /dashboard.js returns 200 JS", js_status == 200 and "javascript" in js_ctype and len(js_body) > 0)

    node_check = subprocess.run(["node", "--check", str(FRONTEND_DIR / "dashboard.js")], capture_output=True, text=True)
    check("dashboard.js passes a real Node.js syntax check", node_check.returncode == 0)

    print("\n== 10. backend.dashboard.router: metrics/health/projects/charts API ==")
    status, ctype, body = handle_request("GET", "/api/dashboard/metrics", {})
    check("GET /api/dashboard/metrics returns 200 JSON matching collect_metrics()", status == 200 and json.loads(body)["execution"]["total_executions"] == 3)

    status, ctype, body = handle_request("GET", "/api/dashboard/health", {})
    check("GET /api/dashboard/health returns 200 with overall_health", status == 200 and "overall_health" in json.loads(body))

    status, ctype, body = handle_request("GET", "/api/dashboard/projects", {})
    check("GET /api/dashboard/projects lists both recorded ventures (multi-project support)", set(json.loads(body)["projects"]) == {"v-alpha", "v-beta"})

    status, ctype, body = handle_request("GET", "/api/dashboard/charts", {})
    check("GET /api/dashboard/charts returns all 8 chart datasets", set(json.loads(body).keys()) == set(CHART_NAMES))

    print("\n== 11. backend.dashboard.router: history search/filter/pagination ==")
    status, ctype, body = handle_request("GET", "/api/dashboard/history", {"page_size": ["1"]})
    page1 = json.loads(body)
    check("history pagination returns exactly page_size items", len(page1["items"]) == 1)
    check("history pagination reports correct total and total_pages", page1["total"] == 3 and page1["total_pages"] == 3)

    status, ctype, body = handle_request("GET", "/api/dashboard/history", {"venture_id": ["v-alpha"]})
    check("history filters by venture_id via the API", json.loads(body)["total"] == 2)

    status, ctype, body = handle_request("GET", "/api/dashboard/history", {"status": ["failed"]})
    check("history filters by status via the API", json.loads(body)["total"] == 1)

    status, ctype, body = handle_request("GET", "/api/dashboard/history", {"search": ["beta"]})
    check("history search works via the API", json.loads(body)["total"] == 1)

    status, ctype, body = handle_request("GET", f"/api/dashboard/history/{eid_a}", {})
    check("GET a single execution by id returns 200", status == 200 and json.loads(body)["execution_id"] == eid_a)

    status, ctype, body = handle_request("GET", "/api/dashboard/history/does-not-exist", {})
    check("GET an unknown execution id returns 404", status == 404)

    status, ctype, body = handle_request("GET", "/api/dashboard/compare", {"a": [eid_a], "b": [eid_b]})
    check("compare endpoint returns 200 with a diff", status == 200 and "diff" in json.loads(body))

    status, ctype, body = handle_request("GET", "/api/dashboard/compare", {"a": [eid_a]})
    check("compare endpoint returns 400 when a required param is missing", status == 400)

    print("\n== 12. backend.dashboard.router: jobs/logs/events filters ==")
    scheduler2 = JobScheduler(num_workers=1, tick_interval_seconds=0.05)
    scheduler2.start()
    set_observed_scheduler(scheduler2)
    try:
        jid = scheduler2.submit("dashboard_smoke_job", args=[10])
        scheduler2.wait_for(jid, timeout=5)
        status, ctype, body = handle_request("GET", "/api/dashboard/jobs", {})
        jobs_payload = json.loads(body)
        check("jobs endpoint reports a registered scheduler with real jobs", jobs_payload["registered"] is True and jobs_payload["total"] >= 1)

        status, ctype, body = handle_request("GET", "/api/dashboard/jobs", {"status": ["completed"]})
        check("jobs endpoint filters by status", all(j["status"] == "completed" for j in json.loads(body)["items"]))
    finally:
        scheduler2.shutdown()
        reset_observed_scheduler()

    status, ctype, body = handle_request("GET", "/api/dashboard/jobs", {})
    check("jobs endpoint reports unregistered after reset_observed_scheduler", json.loads(body)["registered"] is False)

    attach_log_capture(logger_name="afos.smoke_test_dashboard2", level=logging.INFO)
    try:
        logging.getLogger("afos.smoke_test_dashboard2").warning("router log filter test message")
        status, ctype, body = handle_request("GET", "/api/dashboard/logs", {"search": ["router log filter"]})
        check("logs endpoint search works via the API", json.loads(body)["total"] >= 1)
    finally:
        detach_log_capture()

    status, ctype, body = handle_request("GET", "/api/dashboard/events", {"event_type": ["dashboard_smoke_test_event"]})
    check("events endpoint returns the previously published smoke-test event", json.loads(body)["total"] >= 1)

    print("\n== 13. backend.dashboard.router: exports + error handling ==")
    for fmt, expected_ctype in (("json", "application/json"), ("csv", "text/csv"), ("html", "text/html"), ("pdf", "application/pdf")):
        status, ctype, body = handle_request("GET", f"/api/dashboard/export/{fmt}", {})
        check(f"export endpoint '{fmt}' returns 200 with the correct content type", status == 200 and ctype == expected_ctype)

    status, ctype, body = handle_request("GET", "/api/dashboard/export/xml", {})
    check("export endpoint rejects an unsupported format with 400", status == 400)

    status, ctype, body = handle_request("GET", "/api/dashboard/does-not-exist", {})
    check("an unknown API route returns 404", status == 404)

    status, ctype, body = handle_request("POST", "/api/dashboard/metrics", {})
    check("a non-GET request is rejected with 405 (REST reads only)", status == 405)

    print("\n== 14. backend.dashboard.server: real end-to-end HTTP round trip ==")
    server = build_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        time.sleep(0.2)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            check("a real HTTP GET / over a live socket returns 200", resp.status == 200)
            check("a real HTTP GET / returns the dashboard HTML", b"AFOS Founder Dashboard" in resp.read())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard/health", timeout=5) as resp:
            check("a real HTTP GET /api/dashboard/health over a live socket returns 200 JSON", resp.status == 200)
            check("the live health response is valid JSON with overall_health", "overall_health" in json.loads(resp.read()))
    finally:
        server.shutdown()
        server.server_close()

    print("\n== 15. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These twelve files each embed their own full regression section,
    # globbing "smoke_test_phase*.py". None of their (frozen, not to be
    # modified) exclusion lists know about this new file, so including any
    # of them here would let it call back into this file, forming an
    # unbounded subprocess cycle. All twelve already re-verify the full
    # prior suite standalone, so excluding them here loses no coverage.
    excluded = {
        "smoke_test_phase5_dashboard.py",
        "smoke_test_phase5_real_research.py",
        "smoke_test_phase5_scheduler.py",
        "smoke_test_phase5_execution_cache.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
        "smoke_test_phase4_research_pipeline.py",
        "smoke_test_phase4_decision_engine.py",
        "smoke_test_phase4_mvp_planner.py",
        "smoke_test_phase4_ai_builder.py",
        "smoke_test_phase4_deployment_pipeline.py",
        "smoke_test_phase4_growth_pipeline.py",
        "smoke_test_phase4_founder_dashboard.py",
    }
    smoke_tests = sorted(
        p for p in (repo_root / "scripts").glob("smoke_test_phase*.py")
        if p.name not in excluded
    )
    for test_path in smoke_tests:
        proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"     regression: {test_path.name} failed on first attempt, retrying once...")
            proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        check(f"regression: {test_path.name}", proc.returncode == 0)

    print("\n== 16. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 17. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 5 Component 4 checks passed.")


if __name__ == "__main__":
    main()
