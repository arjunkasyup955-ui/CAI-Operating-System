"""AFOS Production Validation Suite.

One command validates the whole system by orchestrating existing,
unmodified AFOS modules and scripts - it never reimplements a pipeline,
provider, or store; every check below either calls an existing AFOS public
function/class directly or shells out to an existing script (scripts/
verify_install.py for LangGraph, scripts/run_afos_demo.py for the full
founder pipeline, scripts/smoke_test_phase*.py for regression).

Modes:
  --quick             Environment, dependency, and provider/storage checks.
                       Fast (seconds to ~1 minute), no product pipeline runs.
  --full              Everything --quick does, plus: the full founder
                       pipeline (via scripts/run_afos_demo.py - Research,
                       Competitor Intelligence, Decision Engine, MVP
                       Planner, AI Builder, Deployment, Dashboard), a
                       deeper workspace/memory round trip, the existing
                       regression/smoke-test suite, and a performance
                       benchmark. Slow (many minutes) and makes real
                       external calls (Tavily/Gemini) - the AI Builder
                       stage also creates a real git commit, exactly like
                       every prior run of scripts/run_afos_demo.py already
                       has (see `git log`).
  --ideas ideas.csv   Runs the Founder Decision Engine (Research Pipeline +
                       scoring, no build/deploy - see --ideas-mode to opt
                       into the full build) for every idea in the CSV and
                       produces a ranked scorecard.

Run (from the repo root, inside .venv):
    python scripts/production_validation.py --quick
    python scripts/production_validation.py --full
    python scripts/production_validation.py --ideas ideas.csv

Reports are written to outputs/: production_validation.{json,md,html},
bug_report.json (always, empty if nothing failed), benchmark.json
(--full only), idea_scorecard.{csv,html} (--ideas only).
"""

from __future__ import annotations

import argparse
import csv
import html as html_lib
import importlib.metadata
import json
import os
import re
import statistics
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
OUTPUTS_DIR = REPO_ROOT / "outputs"

# Anchor the DB path to the repo root before any AFOS module reads it via
# core.config.get_settings() (which resolves AFOS_DB_PATH relative to
# whatever the process's CWD happens to be) - this script must behave
# identically whether invoked from the repo root or elsewhere.
os.environ.setdefault("AFOS_DB_PATH", str(REPO_ROOT / "database" / "afos.db"))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")


# --------------------------------------------------------------------------- #
# Core result model
# --------------------------------------------------------------------------- #


_SECRET_PATTERN = re.compile(r"(?i)\b(key|token|secret|password)=[^&\s'\"]+")


def _redact(text: str) -> str:
    """Strips API-key/token query-param values out of error text before it is
    printed or written to a report file. Several providers here (notably
    Google's REST API) put the API key in the request URL, and httpx's own
    exception __str__ echoes that URL verbatim - without this, a transient
    HTTP error would leak the live key into production_validation.json/
    bug_report.json/console output.
    """
    return _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=***REDACTED***", text)


class ProviderUnavailable(Exception):
    """Raised by a check function when the thing being verified is
    unreachable/exhausted for reasons outside this codebase's control - a
    quota cap, a network failure, an unconfigured optional provider - as
    opposed to a genuine defect (bad auth, malformed input, empty response).
    ValidationRunner.run() records this as a skip ("Skipped (Provider
    Unavailable)"), never a failure, so one provider being down never fails
    the whole validation run.
    """


@dataclass
class CheckResult:
    name: str
    category: str
    status: str  # "pass" | "fail" | "skip"
    detail: str = ""
    error: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "detail": self.detail,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
        }


class ValidationRunner:
    """Executes checks, times them, never lets one check's exception abort
    the run - the same "continue validation even if one provider fails"
    contract every AFOS pipeline component already guarantees for itself.
    """

    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def run(
        self,
        name: str,
        category: str,
        fn: Callable[[], Any],
        skip_reason: Callable[[], str | None] | None = None,
    ) -> CheckResult:
        if skip_reason is not None:
            try:
                reason = skip_reason()
            except Exception as exc:  # noqa: BLE001 - a broken skip-probe should skip, not crash the run
                reason = f"skip-check itself failed: {exc}"
            if reason:
                result = CheckResult(name=name, category=category, status="skip", detail=f"Skipped (Provider Unavailable): {reason}")
                self.results.append(result)
                self._print(result)
                return result

        start = time.monotonic()
        try:
            detail = fn()
            elapsed = time.monotonic() - start
            result = CheckResult(
                name=name, category=category, status="pass",
                detail=str(detail) if detail else "", duration_seconds=round(elapsed, 3),
            )
        except ProviderUnavailable as exc:
            elapsed = time.monotonic() - start
            result = CheckResult(
                name=name, category=category, status="skip",
                detail=f"Skipped (Provider Unavailable): {_redact(str(exc))}", duration_seconds=round(elapsed, 3),
            )
        except Exception as exc:  # noqa: BLE001 - any failure mode must be captured as a result, not raised
            elapsed = time.monotonic() - start
            result = CheckResult(
                name=name, category=category, status="fail",
                error=_redact(str(exc)), duration_seconds=round(elapsed, 3),
            )
        self.results.append(result)
        self._print(result)
        return result

    @staticmethod
    def _print(result: CheckResult) -> None:
        marker = {"pass": "[PASS]", "fail": "[FAIL]", "skip": "[SKIP]"}[result.status]
        extra = result.error or result.detail
        suffix = f" - {extra}" if extra else ""
        print(f"{marker} [{result.category:<11}] {result.name} ({result.duration_seconds:.2f}s){suffix}")


# --------------------------------------------------------------------------- #
# --quick checks
# --------------------------------------------------------------------------- #


def check_python_environment() -> str:
    if sys.version_info < (3, 10):
        raise RuntimeError(f"Python {sys.version.split()[0]} is older than the minimum supported 3.10")
    return f"Python {sys.version.split()[0]} at {sys.executable}"


def check_virtualenv() -> str:
    in_venv = sys.prefix != sys.base_prefix
    if not in_venv:
        raise RuntimeError("not running inside a virtual environment (sys.prefix == sys.base_prefix)")
    project_venv_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    is_project_venv = project_venv_python.exists() and Path(sys.executable).resolve() == project_venv_python.resolve()
    note = "project .venv" if is_project_venv else "a virtual environment other than the project's .venv"
    return f"active: {sys.prefix} ({note})"


def check_required_packages() -> str:
    req_file = REPO_ROOT / "requirements.txt"
    if not req_file.exists():
        raise RuntimeError("requirements.txt not found")

    missing: list[str] = []
    mismatched: list[str] = []
    checked = 0
    for line in req_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, pinned = line.partition("==")
        checked += 1
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
            continue
        if pinned and installed != pinned:
            mismatched.append(f"{name} (installed {installed}, pinned {pinned})")

    if missing:
        raise RuntimeError(f"{len(missing)} package(s) not installed: {', '.join(missing)}")

    detail = f"{checked} pinned packages present"
    if mismatched:
        detail += f"; {len(mismatched)} version drift: {', '.join(mismatched)}"
    return detail


def check_pip_check() -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "check"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=90,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout or proc.stderr).strip() or "pip check failed")
    return "no broken requirements"


def check_git_status() -> str:
    branch_proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    status_proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    if status_proc.returncode != 0:
        raise RuntimeError(status_proc.stderr.strip() or "git status failed")
    branch = branch_proc.stdout.strip() or "unknown"
    dirty = [line for line in status_proc.stdout.splitlines() if line.strip()]
    return f"branch={branch}, {len(dirty)} uncommitted change(s)"


def check_langgraph() -> str:
    """Reuses scripts/verify_install.py verbatim (the existing "9/9" check)
    rather than re-testing LangGraph imports here.
    """
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "verify_install.py")],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    summary = lines[-1] if lines else ""
    if proc.returncode != 0:
        raise RuntimeError(summary or proc.stderr.strip() or "verify_install.py failed")
    return summary


def check_api_keys() -> str:
    keys = {
        "GOOGLE_API_KEY": os.environ.get("GOOGLE_API_KEY", ""),
        "TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY", ""),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "BRAVE_API_KEY": os.environ.get("BRAVE_API_KEY", ""),
    }
    configured = [name for name, value in keys.items() if value]
    required_missing = [name for name in ("GOOGLE_API_KEY", "TAVILY_API_KEY") if not keys[name]]
    if required_missing:
        raise RuntimeError(f"required key(s) not configured: {', '.join(required_missing)}")
    return f"configured: {', '.join(configured) or 'none'}"


def check_gemini() -> str:
    """Retries once on HTTP 429 - a transient per-minute rate limit from this
    same process's other real calls (the --full pipeline, benchmark, or a
    concurrently running regression suite) is resource contention, not
    evidence Gemini itself is misconfigured. A 429 that persists after the
    retry is treated as ProviderUnavailable (skip, not fail) - it's almost
    always a daily free-tier quota cap, an external condition this codebase
    has no control over, distinct from a genuine auth/config defect (401/403,
    which still fails - that IS this codebase's problem to fix).
    """
    import httpx as _httpx

    from core.model_router.providers import GoogleProvider

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            response = GoogleProvider().chat(
                [{"role": "user", "content": "Reply with exactly one word: OK"}], model="gemini-flash-latest",
            )
            if not response.content.strip():
                raise RuntimeError("empty response from Gemini")
            return f"model={response.model} tokens_in={response.input_tokens} tokens_out={response.output_tokens}"
        except _httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code == 429 and attempt == 0:
                time.sleep(5.0)
                continue
            if exc.response.status_code == 429:
                try:
                    message = exc.response.json()["error"]["message"]
                except Exception:
                    message = exc.response.text
                raise ProviderUnavailable(f"Gemini HTTP 429: {message}") from exc
            if exc.response.status_code >= 500:
                raise ProviderUnavailable(f"Gemini HTTP {exc.response.status_code} (upstream server error)") from exc
            raise
        except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
            raise ProviderUnavailable(f"Gemini unreachable: {exc}") from exc
    raise last_exc  # type: ignore[misc]


def check_tavily() -> str:
    """Network failures, 429s, and 5xx responses are ProviderUnavailable
    (skip) - the same distinction as check_gemini: an external outage/quota
    is not evidence this codebase's Tavily integration is broken.
    """
    import httpx as _httpx

    from tools.web.search_providers import TavilyProvider

    try:
        results = TavilyProvider().search("AFOS production validation probe", max_results=1)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429 or exc.response.status_code >= 500:
            raise ProviderUnavailable(f"Tavily HTTP {exc.response.status_code}") from exc
        raise
    except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
        raise ProviderUnavailable(f"Tavily unreachable: {exc}") from exc
    if not results:
        raise RuntimeError("Tavily returned zero results")
    return f"{len(results)} result(s), e.g. {results[0].url}"


def _ollama_unavailable_reason() -> str | None:
    from core.config import get_settings

    base_url = get_settings().ollama_base_url
    try:
        httpx.get(f"{base_url}/api/tags", timeout=2.0)
        return None
    except Exception:
        return f"Ollama not reachable at {base_url} (not installed or not running)"


def check_ollama() -> str:
    from core.config import get_settings

    base_url = get_settings().ollama_base_url
    response = httpx.get(f"{base_url}/api/tags", timeout=5.0)
    response.raise_for_status()
    models = [m.get("name", "?") for m in response.json().get("models", [])]
    return f"{len(models)} model(s): {', '.join(models[:5]) or 'none pulled'}"


def _brave_unconfigured_reason() -> str | None:
    return None if os.environ.get("BRAVE_API_KEY") else "BRAVE_API_KEY not configured"


def check_brave() -> str:
    from tools.web.search_providers import BraveSearchProvider

    results = BraveSearchProvider().search("AFOS production validation probe", max_results=1)
    return f"{len(results)} result(s)"


def _searxng_unavailable_reason() -> str | None:
    base_url = os.environ.get("SEARXNG_BASE_URL", "http://localhost:8080")
    try:
        response = httpx.get(f"{base_url}/search", params={"q": "test", "format": "json"}, timeout=2.0)
        if response.status_code >= 500:
            return f"SearXNG at {base_url} returned HTTP {response.status_code}"
        return None
    except Exception:
        return f"SearXNG not reachable at {base_url} (not running)"


def check_searxng() -> str:
    from tools.web.search_providers import SearxngProvider

    results = SearxngProvider().search("AFOS production validation probe", max_results=1)
    return f"{len(results)} result(s)"


def check_database() -> str:
    from core.memory_gateway.gateway import get_memory_gateway

    gateway = get_memory_gateway()
    row = gateway.knowledge_conn.execute("SELECT 1").fetchone()
    if row != (1,):
        raise RuntimeError("SELECT 1 sanity check failed")
    integrity = gateway.knowledge_conn.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"PRAGMA quick_check reported: {integrity}")
    db_path = Path(os.environ["AFOS_DB_PATH"])
    size_mb = (db_path.stat().st_size / (1024 * 1024)) if db_path.exists() else 0.0
    return f"knowledge_conn OK, quick_check=ok, {db_path.name}={size_mb:.1f}MB"


def check_memory() -> str:
    """Verifies the working-memory checkpointer and the vector store's own
    schema unconditionally (neither needs an external provider). The
    embedding round-trip itself is a separate, conditionally-skipped check
    (see check_vector_embeddings) - VectorStore's default embed_fn calls
    OpenAI, an optional provider this environment does not configure (only
    Google/Tavily are), so failing that here would misreport a genuine
    provider gap as a broken memory tier.
    """
    from core.memory_gateway.gateway import get_memory_gateway

    gateway = get_memory_gateway()
    with gateway.working():
        pass  # LangGraph SqliteSaver context opens and closes cleanly
    if gateway.vector is None:
        raise RuntimeError("vector store not initialized")
    tables = {
        row[0] for row in gateway.knowledge_conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
    }
    required = {"vector_documents", "vector_embeddings"}
    missing = required - tables
    if missing:
        raise RuntimeError(f"vector store schema missing table(s): {', '.join(sorted(missing))}")
    return "working-memory checkpointer OK, vector store schema OK"


def _openai_unconfigured_reason() -> str | None:
    return None if os.environ.get("OPENAI_API_KEY") else (
        "OPENAI_API_KEY not configured - VectorStore's default embed_fn requires it "
        "(this environment uses Google/Tavily; embeddings are an optional tier)"
    )


def check_vector_embeddings() -> str:
    from core.memory_gateway.gateway import get_memory_gateway

    gateway = get_memory_gateway()
    entry_id = gateway.vector.add(
        "AFOS production validation probe entry", venture_id="afos-validation-probe", kind="validation",
    )
    hits = gateway.vector.search("production validation probe", top_k=5)
    if not any(h.get("id") == entry_id for h in hits):
        raise RuntimeError("vector store did not return the entry it was just given")
    return f"add+search round-trip OK (entry {entry_id})"


def check_workspace() -> str:
    from workspace import ProjectStatus, get_default_project_store

    store = get_default_project_store()
    project = store.create_project(
        "AFOS Validation Probe", description="created by scripts/production_validation.py --quick", tags=["validation"],
    )
    pid = project["project_id"]
    try:
        if project["status"] != ProjectStatus.ACTIVE:
            raise RuntimeError("newly created project is not ACTIVE")
        if store.get_project(pid) is None:
            raise RuntimeError("get_project could not find the project just created")
        return f"create/get round-trip OK (probe project {pid})"
    finally:
        store.delete_project(pid)  # soft delete - memory untouched, per ProjectStore's own contract


# --------------------------------------------------------------------------- #
# --full extra checks
# --------------------------------------------------------------------------- #


def check_workspace_deep() -> str:
    from workspace import ProjectStatus, get_default_project_store

    store = get_default_project_store()
    p1 = store.create_project("AFOS Validation Probe A", tags=["validation", "full"])
    p2_id = None
    try:
        assert store.list_projects(tag="validation"), "list_projects(tag=...) found nothing"
        assert store.archive_project(p1["project_id"]), "archive_project failed"
        assert store.get_project(p1["project_id"])["status"] == ProjectStatus.ARCHIVED, "archive did not persist"
        assert store.unarchive_project(p1["project_id"]), "unarchive_project failed"
        clone = store.clone_project(p1["project_id"], new_name="AFOS Validation Probe A (clone)")
        assert clone is not None, "clone_project returned None"
        p2_id = clone["project_id"]
        return f"create/list/archive/unarchive/clone round-trip OK ({p1['project_id']} -> {p2_id})"
    finally:
        store.delete_project(p1["project_id"])
        if p2_id:
            store.delete_project(p2_id)


def check_memory_deep() -> str:
    from workspace import ALL_KINDS, get_default_project_memory_store

    store = get_default_project_memory_store()
    venture_id = "afos-validation-probe-memory"
    for kind in ALL_KINDS:
        store.record(kind, venture_id, {"probe": True, "kind": kind}, source="production_validation")
    try:
        for kind in ALL_KINDS:
            latest = store.get_latest(kind, venture_id)
            if latest is None or latest["data"].get("kind") != kind:
                raise RuntimeError(f"record/get_latest round-trip failed for kind={kind}")
        return f"recorded+verified all {len(ALL_KINDS)} memory kinds for a probe venture"
    finally:
        store.clear_project(venture_id)


_DEMO_IDEA_PATH = REPO_ROOT / "scripts" / "demo_idea.json"
_PIPELINE_STAGE_NAMES = (
    "Founder Orchestrator", "Research Pipeline", "Decision Engine", "MVP Planner",
    "AI Builder", "Deployment Pipeline", "Growth Pipeline", "Founder Dashboard",
)


def run_full_pipeline_via_demo(runner: ValidationRunner, timeout_seconds: float) -> None:
    """Reuses scripts/run_afos_demo.py - which already runs Founder
    Orchestrator -> Research Pipeline (incl. Competitor Intelligence) ->
    Decision Engine -> MVP Planner -> AI Builder -> Deployment Pipeline ->
    Growth Pipeline -> Founder Dashboard end to end and writes outputs/
    integration_report.json - instead of re-implementing that orchestration.
    One check per stage is derived from that report's own per-stage verdicts.

    Note: the AI Builder stage genuinely writes files and creates a real git
    commit ("AI Builder: initial scaffold for <idea>") - this is scripts/
    run_afos_demo.py's own existing, unmodified behavior (see `git log`),
    not something this script adds.
    """
    print("\n>>> Running the full founder pipeline via scripts/run_afos_demo.py")
    print(">>> (this makes real Tavily/Gemini calls and the AI Builder stage will create a git commit)")
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "run_afos_demo.py")],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as timeout_exc:
        elapsed = time.monotonic() - start
        # subprocess.run() (via Popen.communicate) surfaces whatever partial
        # stdout it had accumulated at the moment of the timeout on this
        # exception - the surest way to see which stage was still running
        # without needing to re-run the (many-minutes-long) pipeline again.
        partial_stdout = (timeout_exc.stdout or "").decode("utf-8", "replace") if isinstance(timeout_exc.stdout, bytes) else (timeout_exc.stdout or "")
        stage_markers = [line for line in partial_stdout.splitlines() if line.startswith("=== Stage:") or line.startswith("[")]
        last_stage = stage_markers[-1] if stage_markers else "no stage markers captured before timeout"
        result = CheckResult(
            name="Full founder pipeline (run_afos_demo.py)", category="pipeline", status="fail",
            error=f"timed out after {timeout_seconds:.0f}s - last observed: {last_stage}", duration_seconds=round(elapsed, 3),
        )
        runner.results.append(result)
        runner._print(result)
        for stage in _PIPELINE_STAGE_NAMES:
            skipped = CheckResult(name=stage, category="pipeline", status="skip", detail="parent run_afos_demo.py timed out")
            runner.results.append(skipped)
            runner._print(skipped)
        return

    elapsed = time.monotonic() - start
    report_path = OUTPUTS_DIR / "integration_report.json"
    if proc.returncode not in (0, 1) or not report_path.exists():
        result = CheckResult(
            name="Full founder pipeline (run_afos_demo.py)", category="pipeline", status="fail",
            error=_redact((proc.stderr or proc.stdout or "run_afos_demo.py produced no integration_report.json")[-800:]),
            duration_seconds=round(elapsed, 3),
        )
        runner.results.append(result)
        runner._print(result)
        return

    report = json.loads(report_path.read_text(encoding="utf-8"))
    for stage in report.get("stages", []):
        status = "pass" if stage.get("passed") else "fail"
        result = CheckResult(
            name=stage["stage"], category="pipeline", status=status,
            detail=f"status={stage.get('status')}" if status == "pass" else "",
            error="; ".join(stage.get("errors") or []) if status == "fail" else "",
            duration_seconds=stage.get("execution_time_seconds", 0.0),
        )
        runner.results.append(result)
        runner._print(result)


_RECURSIVE_SUITE_MARKER = 'glob("smoke_test_phase'


def _select_regression_files(smoke_tests: list[Path]) -> list[Path]:
    """A number of the later smoke_test_phase*.py files (detected here by
    grepping for the same glob("smoke_test_phase*.py") marker they use
    internally, rather than a hardcoded list, so this stays correct if more
    are added later) already transitively re-run most/all of the earlier
    files themselves, as their own embedded "regression" section (e.g.
    smoke_test_phase5_real_research.py's docstring: "Full Regression of All
    Previous Phases/Components"). Running every such file in a flat, one-
    subprocess-per-file sweep would mean each one AGAIN re-runs ~30 other
    files as its own subprocesses - up to ~17x redundant, multi-hour
    duplicate execution of the same leaf tests for zero extra signal.

    Keeps every non-recursive ("leaf") file (still-unique, direct per-phase
    coverage) plus exactly one recursive file as a single representative
    full-chain proof.

    Each recursive file's own hardcoded exclusion set only lists files that
    existed when it was written (an incremental-development artifact - a
    smoke test can't know about a smoke test written after it), so most of
    them do NOT exclude their own later-written recursive peers. Chaining
    any of the "early" ones (confirmed empirically: smoke_test_phase5_real_
    research.py, which excludes almost none of its phase5 peers) causes a
    combinatorially-expanding nested subprocess tree - deployment invokes
    workspace, ai_builder_reliability invokes deployment AND workspace AND
    human_approval, etc. - that took over 3.4 hours before this validator's
    own timeout correctly killed it (see production run evidence).

    The fix: pick whichever recursive file's own exclusion set covers the
    MOST of its recursive peers (parsed directly from that file's source,
    not hardcoded) - the most-recently-written one, which by construction
    knows about and excludes all the others, so its own run touches no
    other recursive file and only sweeps the genuine leaf tests once. This
    is a validator efficiency/timeout fix based on each file's own already-
    published exclusion list, not a change to any smoke test file itself.
    """
    leaf: list[Path] = []
    recursive: list[Path] = []
    sources: dict[Path, str] = {}
    for path in smoke_tests:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            text = ""
        sources[path] = text
        (recursive if _RECURSIVE_SUITE_MARKER in text else leaf).append(path)

    representative: list[Path] = []
    if recursive:
        recursive_names = {p.name for p in recursive}
        preferred = max(
            recursive,
            key=lambda p: sum(1 for other in recursive_names if other != p.name and f'"{other}"' in sources[p]),
        )
        representative = [preferred]

    return sorted(leaf) + representative


def run_regression_suite(runner: ValidationRunner, timeout_seconds: float, skip: bool) -> None:
    """Regression + smoke tests, reusing the existing scripts/smoke_test_phase*.py
    suite as-is - this project's own established convention already treats
    these as both. See _select_regression_files() for why the full glob
    isn't run flat (redundant nested re-execution). One subprocess per file;
    a slow/hanging file is timed out and marked failed without blocking the
    rest of the suite.
    """
    if skip:
        result = CheckResult(name="Regression/smoke test suite", category="regression", status="skip", detail="--skip-regression")
        runner.results.append(result)
        runner._print(result)
        return

    all_smoke_tests = sorted((REPO_ROOT / "scripts").glob("smoke_test_phase*.py"))
    smoke_tests = _select_regression_files(all_smoke_tests)
    print(
        f"\n>>> Running {len(smoke_tests)} of {len(all_smoke_tests)} regression/smoke test file(s) "
        f"(timeout={timeout_seconds:.0f}s each; redundant recursive full-suite files collapsed to one representative)"
    )
    for test_path in smoke_tests:
        start = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(test_path)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout_seconds,
            )
            elapsed = time.monotonic() - start
            if proc.returncode == 0:
                result = CheckResult(name=test_path.name, category="regression", status="pass", duration_seconds=round(elapsed, 3))
            else:
                tail = _redact("\n".join((proc.stdout or proc.stderr).strip().splitlines()[-5:]))
                result = CheckResult(name=test_path.name, category="regression", status="fail", error=tail, duration_seconds=round(elapsed, 3))
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            result = CheckResult(name=test_path.name, category="regression", status="fail", error=f"timed out after {timeout_seconds:.0f}s", duration_seconds=round(elapsed, 3))
        runner.results.append(result)
        runner._print(result)


# --------------------------------------------------------------------------- #
# Performance benchmark (--full only)
# --------------------------------------------------------------------------- #


def _time_iterations(fn: Callable[[], Any], iterations: int) -> dict[str, Any]:
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    p95_index = min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))
    return {
        "iterations": iterations,
        "mean_ms": round(statistics.mean(samples), 3),
        "min_ms": round(samples[0], 3),
        "max_ms": round(samples[-1], 3),
        "p95_ms": round(samples[p95_index], 3),
    }


def run_benchmark() -> dict[str, Any]:
    print("\n>>> Running performance benchmark")
    benchmark: dict[str, Any] = {"generated_at": time.time()}

    def _langgraph_invoke() -> None:
        from typing import TypedDict

        from langgraph.graph import END, START, StateGraph

        class _State(TypedDict):
            x: int

        graph = StateGraph(_State)
        graph.add_node("noop", lambda state: {})
        graph.add_edge(START, "noop")
        graph.add_edge("noop", END)
        graph.compile().invoke({"x": 1})

    benchmark["langgraph_trivial_graph_invoke"] = _time_iterations(_langgraph_invoke, 20)

    from workflows.decision_engine import _calculate_scores

    fixed_metrics = {
        "opportunity_priority_score": 0.7, "validation_completed": True, "validation_competition_score": 6.0,
        "direct_competitors_count": 3, "market_gaps_count": 2, "market_risks_count": 1, "validation_risks_count": 1,
        "market_size_tam": "$5B", "cagr": "12%", "opportunity_difficulty": "medium", "validation_execution_score": 6.5,
        "validation_revenue_potential_text": "high", "monetization_model": "subscription", "trend_score": 0.6,
        "emerging_trends_count": 3, "declining_trends_count": 1, "validation_timing_score": 7.0,
        "idea_text": "an AI-powered voice agent for SMBs", "market_confidence": 0.7, "competitor_confidence": 0.6,
        "trend_confidence": 0.65, "opportunity_confidence": 0.7, "validation_confidence": 0.75, "stage_success_ratio": 1.0,
    }
    benchmark["decision_engine_pure_scoring"] = _time_iterations(lambda: _calculate_scores(fixed_metrics), 200)

    from core.memory_gateway.gateway import get_memory_gateway

    gateway = get_memory_gateway()
    gateway.knowledge_conn.execute(
        "CREATE TABLE IF NOT EXISTS validation_benchmark_probe (id INTEGER PRIMARY KEY, value TEXT)"
    )

    def _sqlite_roundtrip() -> None:
        gateway.knowledge_conn.execute("INSERT INTO validation_benchmark_probe (value) VALUES (?)", ("probe",))
        gateway.knowledge_conn.execute("SELECT value FROM validation_benchmark_probe ORDER BY id DESC LIMIT 1").fetchone()

    benchmark["sqlite_roundtrip"] = _time_iterations(_sqlite_roundtrip, 50)
    gateway.knowledge_conn.execute("DROP TABLE validation_benchmark_probe")
    gateway.knowledge_conn.commit()

    try:
        from tools.web.search_providers import TavilyProvider

        start = time.perf_counter()
        TavilyProvider().search("AFOS benchmark probe", max_results=1)
        benchmark["tavily_search_single_call_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
    except Exception as exc:
        benchmark["tavily_search_single_call_ms"] = None
        benchmark["tavily_search_error"] = _redact(str(exc))

    try:
        from core.model_router.providers import GoogleProvider

        start = time.perf_counter()
        GoogleProvider().chat([{"role": "user", "content": "Reply with exactly one word: OK"}], model="gemini-flash-latest")
        benchmark["gemini_chat_single_call_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
    except Exception as exc:
        benchmark["gemini_chat_single_call_ms"] = None
        benchmark["gemini_chat_error"] = _redact(str(exc))

    return benchmark


# --------------------------------------------------------------------------- #
# --ideas scorecard mode
# --------------------------------------------------------------------------- #


_SCORE_KEYS = (
    "opportunity_score", "competition_score", "risk_score", "market_size_score", "build_difficulty",
    "execution_complexity", "revenue_potential", "market_timing", "ai_advantage", "confidence_score",
)


def _slugify(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or fallback


def run_ideas_mode(csv_path: Path, mode: str) -> list[dict[str, Any]]:
    """Runs the Founder Decision Engine (mode="decision", the default - real
    research + real scoring, no build/deploy/git side effects) or the full
    Founder Dashboard (mode="dashboard" - includes AI Builder/git commits and
    Deployment, one full pipeline run per idea) for every row in the CSV.
    Reuses agents.founder.decision_engine/dashboard's own public functions
    unmodified; this function only loops and shapes the scorecard.
    """
    if mode == "dashboard":
        from agents.founder.dashboard.agent import run_founder_dashboard as _run
    else:
        from agents.founder.decision_engine.agent import run_decision_engine as _run

    rows: list[dict[str, Any]] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for index, row in enumerate(reader):
            idea = (row.get("idea") or "").strip()
            if not idea:
                continue
            venture_id = (row.get("venture_id") or "").strip() or f"scorecard-{index}-{_slugify(idea, f'idea-{index}')}"
            research_depth = (row.get("research_depth") or "standard").strip() or "standard"
            rows.append({"idea": idea, "venture_id": venture_id, "research_depth": research_depth})

    print(f"\n>>> Running {mode} mode for {len(rows)} idea(s) from {csv_path.name}")
    scorecard: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        print(f"  [{i}/{len(rows)}] {row['idea'][:70]!r}")
        start = time.monotonic()
        try:
            result = _run(row["idea"], row["venture_id"], row["research_depth"])
            elapsed = time.monotonic() - start
            if mode == "dashboard":
                decision = result.get("decision_scores", {})
                entry = {
                    "idea": row["idea"], "venture_id": row["venture_id"],
                    **{key: decision.get(key, 0.0) for key in _SCORE_KEYS},
                    "overall_score": decision.get("overall_score", 0.0),
                    "recommendation": decision.get("recommendation", "unknown"),
                    "status": result.get("status", "unknown"),
                    "build_status": result.get("build_status", {}).get("status", "unknown"),
                    "deployment_status": result.get("deployment_status", {}).get("status", "unknown"),
                    "top_reason": "", "top_warning": (result.get("alerts") or [""])[0],
                    "execution_time_seconds": round(elapsed, 3), "error": "",
                }
            else:
                entry = {
                    "idea": row["idea"], "venture_id": row["venture_id"],
                    **{key: result.get(key, 0.0) for key in _SCORE_KEYS},
                    "overall_score": result.get("overall_score", 0.0),
                    "recommendation": result.get("recommendation", "unknown"),
                    "status": result.get("status", "unknown"),
                    "top_reason": (result.get("reasons") or [""])[0],
                    "top_warning": (result.get("warnings") or [""])[0],
                    "execution_time_seconds": round(elapsed, 3), "error": "",
                }
        except Exception as exc:
            elapsed = time.monotonic() - start
            entry = {
                "idea": row["idea"], "venture_id": row["venture_id"],
                **{key: 0.0 for key in _SCORE_KEYS}, "overall_score": 0.0, "recommendation": "ERROR",
                "status": "exception", "top_reason": "", "top_warning": "",
                "execution_time_seconds": round(elapsed, 3), "error": str(exc),
            }
        scorecard.append(entry)

    scorecard.sort(key=lambda e: e["overall_score"], reverse=True)
    return scorecard


def write_idea_scorecard_csv(scorecard: list[dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "idea", "venture_id", "overall_score", "recommendation", "status",
        *_SCORE_KEYS, "top_reason", "top_warning", "execution_time_seconds", "error",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for entry in scorecard:
            writer.writerow({key: entry.get(key, "") for key in fieldnames})


def write_idea_scorecard_html(scorecard: list[dict[str, Any]], path: Path) -> None:
    def esc(value: Any) -> str:
        return html_lib.escape(str(value), quote=True)

    rec_colors = {"BUILD NOW": "#1a7f37", "VALIDATE FIRST": "#9a6700", "PIVOT": "#bf8700", "DROP": "#cf222e", "ERROR": "#6e7781", "unknown": "#6e7781"}
    row_parts = []
    for i, e in enumerate(scorecard):
        color = rec_colors.get(e["recommendation"], "#6e7781")
        row_parts.append(
            f"<tr><td>{i + 1}</td><td>{esc(e['idea'])}</td><td>{e['overall_score']:.2f}</td>"
            f"<td><span class='badge' style='background:{color}'>{esc(e['recommendation'])}</span></td>"
            f"<td>{esc(e['status'])}</td><td>{esc(e['top_reason'] or e['error'])}</td><td>{e['execution_time_seconds']:.1f}s</td></tr>"
        )
    rows = "".join(row_parts)
    path.write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><title>AFOS Idea Scorecard</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 0; background: #0d1117; color: #e6edf3; }}
  header {{ padding: 24px 32px; background: linear-gradient(135deg, #1f6feb, #388bfd); }}
  header h1 {{ margin: 0; font-size: 26px; }}
  main {{ padding: 24px 32px; max-width: 1200px; margin: 0 auto; }}
  table {{ width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #30363d; }}
  th {{ color: #8b949e; font-size: 12px; text-transform: uppercase; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; color: white; font-weight: 600; font-size: 12px; }}
  footer {{ text-align: center; color: #6e7781; padding: 20px; font-size: 12px; }}
</style></head><body>
<header><h1>AFOS Idea Scorecard</h1></header>
<main><table>
<tr><th>#</th><th>Idea</th><th>Overall Score</th><th>Recommendation</th><th>Status</th><th>Top Reason / Error</th><th>Time</th></tr>
{rows}
</table></main>
<footer>Generated by scripts/production_validation.py --ideas</footer>
</body></html>""", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Report writers
# --------------------------------------------------------------------------- #


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    skipped = sum(1 for r in results if r.status == "skip")
    scored = passed + failed
    health_score = round((passed / scored) * 100.0, 1) if scored else 100.0
    return {
        "total_checks": len(results), "passed": passed, "failed": failed, "skipped": skipped,
        "health_score": health_score, "final_result": "PASS" if failed == 0 else "FAIL",
    }


def write_json_report(payload: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def write_markdown_report(payload: dict[str, Any], path: Path) -> None:
    summary = payload["summary"]
    lines = [
        "# AFOS Production Validation Report", "",
        f"- **Mode:** {payload['mode']}",
        f"- **Generated:** {payload['generated_at']}",
        f"- **Total execution time:** {payload['total_execution_time_seconds']:.2f}s",
        f"- **Checks:** {summary['passed']} passed / {summary['failed']} failed / {summary['skipped']} skipped (of {summary['total_checks']})",
        f"- **Health score:** {summary['health_score']}%",
        f"- **Result:** {summary['final_result']}", "",
        "| Category | Check | Status | Time (s) | Detail / Error |",
        "|---|---|---|---|---|",
    ]
    for check in payload["checks"]:
        marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[check["status"]]
        note = (check["error"] or check["detail"]).replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {check['category']} | {check['name']} | {marker} | {check['duration_seconds']:.2f} | {note} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html_report(payload: dict[str, Any], path: Path) -> None:
    def esc(value: Any) -> str:
        return html_lib.escape(str(value), quote=True)

    summary = payload["summary"]
    status_colors = {"pass": "#1a7f37", "fail": "#cf222e", "skip": "#9a6700"}
    rows = "".join(
        f"<tr><td>{esc(c['category'])}</td><td>{esc(c['name'])}</td>"
        f"<td><span class='badge' style='background:{status_colors[c['status']]}'>{c['status'].upper()}</span></td>"
        f"<td>{c['duration_seconds']:.2f}s</td><td>{esc(c['error'] or c['detail'])}</td></tr>"
        for c in payload["checks"]
    )
    result_color = "#1a7f37" if summary["final_result"] == "PASS" else "#cf222e"
    path.write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><title>AFOS Production Validation</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 0; background: #0d1117; color: #e6edf3; }}
  header {{ padding: 24px 32px; background: linear-gradient(135deg, #1f6feb, #388bfd); }}
  header h1 {{ margin: 0; font-size: 26px; }}
  main {{ padding: 24px 32px; max-width: 1200px; margin: 0 auto; }}
  section {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 18px 22px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #30363d; font-size: 14px; }}
  th {{ color: #8b949e; font-size: 12px; text-transform: uppercase; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; color: white; font-weight: 600; font-size: 12px; }}
  .score-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }}
  .score-card {{ background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px 14px; }}
  .score-card .label {{ color: #8b949e; font-size: 12px; }}
  .score-card .value {{ font-size: 22px; font-weight: 700; }}
  footer {{ text-align: center; color: #6e7781; padding: 20px; font-size: 12px; }}
</style></head><body>
<header><h1>AFOS Production Validation &mdash; {esc(payload['mode'])}</h1></header>
<main>
<section>
  <div class="score-grid">
    <div class="score-card"><div class="label">Result</div><div class="value" style="color:{result_color}">{esc(summary['final_result'])}</div></div>
    <div class="score-card"><div class="label">Health Score</div><div class="value">{summary['health_score']}%</div></div>
    <div class="score-card"><div class="label">Passed</div><div class="value">{summary['passed']}</div></div>
    <div class="score-card"><div class="label">Failed</div><div class="value">{summary['failed']}</div></div>
    <div class="score-card"><div class="label">Skipped</div><div class="value">{summary['skipped']}</div></div>
    <div class="score-card"><div class="label">Total Time</div><div class="value">{payload['total_execution_time_seconds']:.1f}s</div></div>
  </div>
</section>
<section><h2>Checks</h2><table>
<tr><th>Category</th><th>Check</th><th>Status</th><th>Time</th><th>Detail / Error</th></tr>
{rows}
</table></section>
</main>
<footer>Generated by scripts/production_validation.py &mdash; mode={esc(payload['mode'])} at {esc(payload['generated_at'])}</footer>
</body></html>""", encoding="utf-8")


def write_bug_report(results: list[CheckResult], path: Path) -> None:
    bugs = [r.to_dict() for r in results if r.status == "fail"]
    path.write_text(json.dumps({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "bug_count": len(bugs), "bugs": bugs}, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run_quick_checks(runner: ValidationRunner) -> None:
    runner.run("Python environment", "environment", check_python_environment)
    runner.run("Virtual environment", "environment", check_virtualenv)
    runner.run("Required packages", "environment", check_required_packages)
    runner.run("pip check", "environment", check_pip_check)
    runner.run("Git status", "environment", check_git_status)
    runner.run("LangGraph verification", "environment", check_langgraph)
    runner.run("API key verification", "providers", check_api_keys)
    runner.run("Gemini verification", "providers", check_gemini)
    runner.run("Tavily verification", "providers", check_tavily)
    runner.run("Ollama verification", "providers", check_ollama, skip_reason=_ollama_unavailable_reason)
    runner.run("Brave verification", "providers", check_brave, skip_reason=_brave_unconfigured_reason)
    runner.run("SearXNG verification", "providers", check_searxng, skip_reason=_searxng_unavailable_reason)
    runner.run("Database verification", "storage", check_database)
    runner.run("Workspace verification", "storage", check_workspace)
    runner.run("Memory verification", "storage", check_memory)
    runner.run("Vector embeddings verification", "storage", check_vector_embeddings, skip_reason=_openai_unconfigured_reason)


def print_summary(payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    print("\n" + "=" * 72)
    print("AFOS Production Validation Suite - Summary")
    print("=" * 72)
    print(f"Mode:              {payload['mode']}")
    print(f"Execution time:    {payload['total_execution_time_seconds']:.2f}s")
    print(f"Checks passed:     {summary['passed']}")
    print(f"Checks failed:     {summary['failed']}")
    print(f"Checks skipped:    {summary['skipped']}")
    print(f"Health score:      {summary['health_score']}%")
    print(f"Final result:      {summary['final_result']}")
    print("=" * 72)


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)  # visible progress when stdout is redirected to a file/log, not just a TTY
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="AFOS Production Validation Suite")
    parser.add_argument("--quick", action="store_true", help="Environment/provider/storage checks")
    parser.add_argument("--full", action="store_true", help="--quick plus full pipeline, regression, and benchmark")
    parser.add_argument("--ideas", metavar="CSV", help="Run a scorecard over every idea in the given CSV")
    parser.add_argument("--ideas-mode", choices=["decision", "dashboard"], default="decision",
                         help="decision (default): Decision Engine only, no build/deploy/git side effects. "
                              "dashboard: full Founder Dashboard per idea, including AI Builder git commits.")
    parser.add_argument("--pipeline-timeout", type=float, default=1800.0, help="Timeout in seconds for the --full pipeline run (raise this in environments falling back to local Ollama, which is much slower than a configured cloud LLM)")
    parser.add_argument("--regression-timeout", type=float, default=3600.0, help="Per-file timeout in seconds for regression/smoke tests (the one representative recursive file - see _select_regression_files - internally re-runs ~30 other files itself, so needs a long budget, especially under local Ollama fallback)")
    parser.add_argument("--skip-regression", action="store_true", help="Skip the regression/smoke test suite in --full")
    args = parser.parse_args()

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    if not args.quick and not args.full and not args.ideas:
        args.quick = True
        print("No mode specified - defaulting to --quick.\n")

    overall_start = time.monotonic()

    if args.ideas:
        csv_path = Path(args.ideas)
        if not csv_path.is_absolute():
            csv_path = REPO_ROOT / csv_path
        if not csv_path.exists():
            print(f"[FAIL] ideas CSV not found: {csv_path}")
            return 1
        scorecard = run_ideas_mode(csv_path, args.ideas_mode)
        write_idea_scorecard_csv(scorecard, OUTPUTS_DIR / "idea_scorecard.csv")
        write_idea_scorecard_html(scorecard, OUTPUTS_DIR / "idea_scorecard.html")

        checks = [
            CheckResult(
                name=e["idea"][:80], category="ideas",
                status="fail" if e["status"] == "exception" else "pass",
                detail=f"overall_score={e['overall_score']} recommendation={e['recommendation']}",
                error=e["error"], duration_seconds=e["execution_time_seconds"],
            )
            for e in scorecard
        ]
        runner = ValidationRunner()
        runner.results = checks
        total_elapsed = time.monotonic() - overall_start
        payload = {
            "mode": f"ideas ({args.ideas_mode})", "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_execution_time_seconds": round(total_elapsed, 3),
            "checks": [c.to_dict() for c in runner.results], "summary": summarize(runner.results),
            "idea_count": len(scorecard),
        }
        write_json_report(payload, OUTPUTS_DIR / "production_validation.json")
        write_markdown_report(payload, OUTPUTS_DIR / "production_validation.md")
        write_html_report(payload, OUTPUTS_DIR / "production_validation.html")
        write_bug_report(runner.results, OUTPUTS_DIR / "bug_report.json")
        print_summary(payload)
        print(f"\nScorecard: {OUTPUTS_DIR / 'idea_scorecard.csv'}")
        print(f"Scorecard: {OUTPUTS_DIR / 'idea_scorecard.html'}")
        return 0 if payload["summary"]["failed"] == 0 else 1

    runner = ValidationRunner()
    mode = "full" if args.full else "quick"

    print("=" * 72)
    print(f"AFOS Production Validation Suite - mode={mode}")
    print("=" * 72)

    run_quick_checks(runner)

    benchmark: dict[str, Any] | None = None
    if args.full:
        # Regression runs BEFORE the founder-pipeline stage, not after: one
        # of the regression suite's own frozen files (smoke_test_phase5_
        # workspace.py) asserts a clean git tree, and the pipeline stage
        # below (via scripts/run_afos_demo.py) always rewrites outputs/
        # founder_dashboard.{html,json} and outputs/integration_report.json
        # as its own report artifacts. Running regression first means that
        # assertion sees the tree in whatever state it was in before this
        # invocation, not dirtied by this same run's own later pipeline step.
        run_regression_suite(runner, args.regression_timeout, args.skip_regression)
        run_full_pipeline_via_demo(runner, args.pipeline_timeout)
        runner.run("Workspace verification (deep)", "storage", check_workspace_deep)
        runner.run("Memory verification (deep)", "storage", check_memory_deep)
        try:
            benchmark = run_benchmark()
            write_json_report(benchmark, OUTPUTS_DIR / "benchmark.json")
            print(f"Benchmark written to {OUTPUTS_DIR / 'benchmark.json'}")
        except Exception as exc:
            print(f"[FAIL] performance benchmark raised: {exc}")
            traceback.print_exc()
            runner.results.append(CheckResult(name="Performance benchmark", category="benchmark", status="fail", error=str(exc)))

    total_elapsed = time.monotonic() - overall_start
    payload = {
        "mode": mode, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_execution_time_seconds": round(total_elapsed, 3),
        "checks": [c.to_dict() for c in runner.results], "summary": summarize(runner.results),
    }
    write_json_report(payload, OUTPUTS_DIR / "production_validation.json")
    write_markdown_report(payload, OUTPUTS_DIR / "production_validation.md")
    write_html_report(payload, OUTPUTS_DIR / "production_validation.html")
    write_bug_report(runner.results, OUTPUTS_DIR / "bug_report.json")

    print_summary(payload)
    print(f"\nReports written to: {OUTPUTS_DIR}")
    for name in ("production_validation.json", "production_validation.md", "production_validation.html", "bug_report.json"):
        print(f"  - {name}")
    if benchmark is not None:
        print("  - benchmark.json")

    return 0 if payload["summary"]["final_result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
