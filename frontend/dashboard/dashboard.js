(function () {
  "use strict";

  var API_BASE = "/api/dashboard";
  var CHART_KEYS = [
    "pipeline_timeline", "execution_time", "cache_hit_rate", "research_confidence",
    "competitor_count", "job_queue", "health_score", "overall_score",
  ];

  var state = {
    project: "",
    history: { page: 1, pageSize: 10, search: "", status: "" },
    jobs: { page: 1, pageSize: 10, search: "", status: "" },
    logs: { page: 1, pageSize: 10, search: "", category: "" },
    events: { page: 1, pageSize: 10 },
  };
  var charts = {};

  function qs(params) {
    var parts = [];
    Object.keys(params).forEach(function (key) {
      var value = params[key];
      if (value !== undefined && value !== null && value !== "") {
        parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(value));
      }
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  function fetchJSON(path, params) {
    return fetch(path + qs(params || {}))
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (err) {
            throw new Error(err.error || ("HTTP " + res.status));
          });
        }
        return res.json();
      });
  }

  function postJSON(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
        return data;
      });
    });
  }

  function el(html) {
    var wrap = document.createElement("div");
    wrap.innerHTML = html.trim();
    return wrap.firstChild;
  }

  function escapeHtml(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function showLoading(visible) {
    var overlay = document.getElementById("loading-overlay");
    if (overlay) overlay.classList.toggle("hidden", !visible);
  }

  function notify(message, type) {
    var container = document.getElementById("notifications");
    if (!container) return;
    var toast = el('<div class="toast ' + (type || "") + '">' + escapeHtml(message) + "</div>");
    container.appendChild(toast);
    setTimeout(function () {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 5000);
  }

  // ---------------------------------------------------------------- //
  // Collapsible cards
  // ---------------------------------------------------------------- //
  function setupCollapsibleCards() {
    document.querySelectorAll(".card-header").forEach(function (header) {
      header.addEventListener("click", function () {
        header.closest(".card").classList.toggle("collapsed");
      });
    });
  }

  // ---------------------------------------------------------------- //
  // Sidebar nav
  // ---------------------------------------------------------------- //
  function setupSidebarNav() {
    document.querySelectorAll(".nav-link").forEach(function (link) {
      link.addEventListener("click", function () {
        document.querySelectorAll(".nav-link").forEach(function (l) { l.classList.remove("active"); });
        link.classList.add("active");
      });
    });
  }

  // ---------------------------------------------------------------- //
  // Health / overview
  // ---------------------------------------------------------------- //
  function statusClass(status) {
    return ["healthy", "degraded", "critical"].indexOf(status) >= 0 ? status : "unknown";
  }

  function renderHealth(health) {
    var grid = document.getElementById("health-grid");
    var dims = [
      ["Overall", health.overall_health, health.overall_score],
      ["Pipeline", health.pipeline_health, null],
      ["Scheduler", health.scheduler_health, null],
      ["Research", health.research_health, null],
      ["Builder", health.builder_health, null],
      ["Deployment", health.deployment_health, null],
    ];
    grid.innerHTML = "";
    dims.forEach(function (d) {
      var name = d[0], status = d[1], score = d[2];
      var cls = statusClass(status);
      var tile = el(
        '<div class="health-tile">' +
        '<div class="label">' + escapeHtml(name) + " Health</div>" +
        '<div class="value"><span class="status-dot ' + cls + '"></span>' + escapeHtml(status || "unknown") + "</div>" +
        (score !== null ? '<div class="progress-bar"><div class="progress-bar-fill ' + cls + '" style="width:' + Math.round(score * 100) + '%"></div></div>' : "") +
        "</div>"
      );
      grid.appendChild(tile);
    });
    var dot = document.getElementById("overall-status-dot");
    if (dot) dot.className = "status-dot " + statusClass(health.overall_health);
  }

  // ---------------------------------------------------------------- //
  // Metrics
  // ---------------------------------------------------------------- //
  function renderMetrics(metrics) {
    var grid = document.getElementById("metrics-grid");
    var execu = metrics.execution || {};
    var cache = metrics.cache || {};
    var sched = metrics.scheduler || {};
    var tiles = [
      ["Total Executions", execu.total_executions],
      ["Completed", execu.completed],
      ["Failed", execu.failed],
      ["Running", execu.running],
      ["Avg Execution Time (s)", execu.average_execution_time_seconds],
      ["Avg Confidence Score", execu.average_confidence_score],
      ["Avg Competitor Count", execu.average_competitor_count],
      ["Cache Hit Rate", cache.hit_rate],
      ["Cache Size", cache.cache_size],
      ["Total Jobs", sched.total_jobs],
      ["Queue Size", sched.queue_size],
      ["Scheduler Registered", sched.registered],
    ];
    grid.innerHTML = "";
    tiles.forEach(function (t) {
      grid.appendChild(el(
        '<div class="stat-tile"><div class="label">' + escapeHtml(t[0]) + '</div><div class="value">' + escapeHtml(t[1]) + "</div></div>"
      ));
    });
  }

  // ---------------------------------------------------------------- //
  // Charts (Chart.js)
  // ---------------------------------------------------------------- //
  function renderCharts(chartData) {
    if (typeof Chart === "undefined") return; // Chart.js CDN unavailable (e.g. offline) - dashboard still functions
    CHART_KEYS.forEach(function (key) {
      var canvas = document.getElementById("chart-" + key);
      var spec = chartData[key];
      if (!canvas || !spec) return;
      if (charts[key]) charts[key].destroy();
      charts[key] = new Chart(canvas.getContext("2d"), {
        type: spec.type,
        data: { labels: spec.labels, datasets: spec.datasets },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: "#e5e7eb" } } } },
      });
    });
  }

  // ---------------------------------------------------------------- //
  // Generic paginated table/list renderer
  // ---------------------------------------------------------------- //
  function renderPagination(containerId, page, totalPages, onChange) {
    var container = document.getElementById(containerId);
    container.innerHTML = "";
    var prev = el('<button' + (page <= 1 ? " disabled" : "") + ">Prev</button>");
    var info = el('<span>Page ' + page + " of " + Math.max(totalPages, 1) + "</span>");
    var next = el('<button' + (page >= totalPages ? " disabled" : "") + ">Next</button>");
    prev.addEventListener("click", function () { onChange(page - 1); });
    next.addEventListener("click", function () { onChange(page + 1); });
    container.appendChild(prev);
    container.appendChild(info);
    container.appendChild(next);
  }

  // ---------------------------------------------------------------- //
  // History
  // ---------------------------------------------------------------- //
  function loadHistory() {
    return fetchJSON(API_BASE + "/history", {
      venture_id: state.project, search: state.history.search, status: state.history.status,
      page: state.history.page, page_size: state.history.pageSize,
    }).then(function (data) {
      var wrap = document.getElementById("history-table-wrap");
      if (!data.items.length) {
        wrap.innerHTML = '<div class="empty-state">No executions recorded yet.</div>';
      } else {
        var rows = data.items.map(function (e) {
          return "<tr><td>" + escapeHtml(e.execution_id.slice(0, 8)) + "</td><td>" + escapeHtml(e.pipeline_name) +
            "</td><td>" + escapeHtml(e.venture_id) + '</td><td><span class="badge ' + escapeHtml(e.status) + '">' + escapeHtml(e.status) +
            "</span></td><td>" + escapeHtml(e.execution_time_seconds) + "</td><td>" + escapeHtml(e.confidence_score) +
            "</td><td>" + escapeHtml(e.competitor_count) + "</td></tr>";
        }).join("");
        wrap.innerHTML = '<table class="data-table"><thead><tr><th>ID</th><th>Pipeline</th><th>Venture</th><th>Status</th>' +
          "<th>Time (s)</th><th>Confidence</th><th>Competitors</th></tr></thead><tbody>" + rows + "</tbody></table>";
      }
      renderPagination("history-pagination", data.page, data.total_pages, function (p) {
        state.history.page = p; loadHistory();
      });
      return data;
    });
  }

  // ---------------------------------------------------------------- //
  // Jobs
  // ---------------------------------------------------------------- //
  function loadJobs() {
    return fetchJSON(API_BASE + "/jobs", {
      search: state.jobs.search, status: state.jobs.status, page: state.jobs.page, page_size: state.jobs.pageSize,
    }).then(function (data) {
      var wrap = document.getElementById("jobs-table-wrap");
      if (!data.registered) {
        wrap.innerHTML = '<div class="empty-state">No scheduler registered for this dashboard instance.</div>';
      } else if (!data.items.length) {
        wrap.innerHTML = '<div class="empty-state">No jobs found.</div>';
      } else {
        var rows = data.items.map(function (j) {
          return "<tr><td>" + escapeHtml(j.job_id.slice(0, 8)) + "</td><td>" + escapeHtml(j.job_type) +
            '</td><td><span class="badge ' + escapeHtml(j.status) + '">' + escapeHtml(j.status) + "</span></td><td>" +
            escapeHtml(j.progress_percent) + "%</td><td>" + escapeHtml(j.attempts) + "</td></tr>";
        }).join("");
        wrap.innerHTML = '<table class="data-table"><thead><tr><th>ID</th><th>Type</th><th>Status</th><th>Progress</th><th>Attempts</th></tr></thead><tbody>' + rows + "</tbody></table>";
      }
      renderPagination("jobs-pagination", data.page, data.total_pages, function (p) {
        state.jobs.page = p; loadJobs();
      });
      return data;
    });
  }

  // ---------------------------------------------------------------- //
  // Logs
  // ---------------------------------------------------------------- //
  function loadLogs() {
    return fetchJSON(API_BASE + "/logs", {
      search: state.logs.search, category: state.logs.category, page: state.logs.page, page_size: state.logs.pageSize,
    }).then(function (data) {
      var wrap = document.getElementById("logs-list-wrap");
      if (!data.items.length) {
        wrap.innerHTML = '<div class="empty-state">No log entries captured yet.</div>';
      } else {
        var rows = data.items.map(function (l) {
          return "<tr><td>" + escapeHtml(l.logger) + '</td><td><span class="badge ' + escapeHtml(l.category) + '">' +
            escapeHtml(l.category) + "</span></td><td>" + escapeHtml(l.level) + "</td><td>" + escapeHtml(l.message) + "</td></tr>";
        }).join("");
        wrap.innerHTML = '<table class="data-table"><thead><tr><th>Logger</th><th>Category</th><th>Level</th><th>Message</th></tr></thead><tbody>' + rows + "</tbody></table>";
      }
      renderPagination("logs-pagination", data.page, data.total_pages, function (p) {
        state.logs.page = p; loadLogs();
      });
      return data;
    });
  }

  // ---------------------------------------------------------------- //
  // Events
  // ---------------------------------------------------------------- //
  function loadEvents() {
    return fetchJSON(API_BASE + "/events", {
      venture_id: state.project, page: state.events.page, page_size: state.events.pageSize,
    }).then(function (data) {
      var wrap = document.getElementById("events-list-wrap");
      if (!data.items.length) {
        wrap.innerHTML = '<div class="empty-state">No pipeline events recorded yet.</div>';
      } else {
        var rows = data.items.map(function (e) {
          return "<tr><td>" + escapeHtml(e.type) + "</td><td>" + escapeHtml(e.source_agent) + "</td><td>" +
            escapeHtml(e.venture_id) + "</td><td>" + escapeHtml(e.timestamp) + "</td></tr>";
        }).join("");
        wrap.innerHTML = '<table class="data-table"><thead><tr><th>Type</th><th>Source</th><th>Venture</th><th>Timestamp</th></tr></thead><tbody>' + rows + "</tbody></table>";
      }
      renderPagination("events-pagination", data.page, data.total_pages, function (p) {
        state.events.page = p; loadEvents();
      });
      return data;
    });
  }

  // ---------------------------------------------------------------- //
  // Projects / project selector
  // ---------------------------------------------------------------- //
  function loadProjects() {
    return fetchJSON(API_BASE + "/projects").then(function (data) {
      var select = document.getElementById("project-selector");
      var current = select.value;
      Array.from(select.querySelectorAll("option[data-dynamic]")).forEach(function (o) { o.remove(); });
      (data.projects || []).forEach(function (p) {
        var opt = document.createElement("option");
        opt.value = p; opt.textContent = p; opt.setAttribute("data-dynamic", "1");
        select.appendChild(opt);
      });
      select.value = current;
      return data;
    });
  }

  // ---------------------------------------------------------------- //
  // Export
  // ---------------------------------------------------------------- //
  function setupExportButtons() {
    document.querySelectorAll(".export-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var fmt = btn.getAttribute("data-format");
        var url = API_BASE + "/export/" + fmt + qs({ venture_id: state.project });
        window.open(url, "_blank");
        notify("Export started: " + fmt.toUpperCase(), "success");
      });
    });
  }

  // ---------------------------------------------------------------- //
  // Submit idea + live status polling
  //
  // Server-side, submissions run one at a time (a single background worker
  // drains a queue - see backend/dashboard/jobs.py) - not just to be polite
  // to the NIM rate limit, but because the pipeline caches research results
  // in a shared global that isn't safe to read from two concurrent runs.
  // So a second submission while one is in flight is expected to sit as
  // "queued" here, not an error.
  // ---------------------------------------------------------------- //
  var submitState = { jobId: null, pollTimer: null };

  function stageToPercent(stage, status) {
    if (status === "done") return 100;
    if (status === "error") return 100;
    var m = /\((\d+)\/(\d+)\)/.exec(stage || "");
    if (m) return Math.round((parseInt(m[1], 10) / parseInt(m[2], 10)) * 100);
    if (stage === "finishing up") return 95;
    if (stage === "starting") return 5;
    return 0;
  }

  function renderJobStatus(job) {
    var wrap = document.getElementById("submit-status-wrap");
    var badge = document.getElementById("submit-status-badge");
    var stageEl = document.getElementById("submit-status-stage");
    var progress = document.getElementById("submit-status-progress");
    var detail = document.getElementById("submit-status-detail");

    wrap.classList.remove("hidden");
    badge.className = "badge " + escapeHtml(job.status);
    badge.textContent = job.status;
    stageEl.textContent = job.current_stage || "";

    progress.className = "progress-bar-fill" +
      (job.status === "done" ? " healthy" : job.status === "error" ? " critical" : "");
    progress.style.width = stageToPercent(job.current_stage, job.status) + "%";

    if (job.status === "queued" && job.queue_position > 0) {
      detail.textContent = job.queue_position + " idea(s) ahead of this one - it will start automatically.";
    } else if (job.status === "error") {
      detail.textContent = job.error || "The pipeline reported a failure.";
    } else if (job.status === "done") {
      var scores = (job.result && job.result.decision_scores) || {};
      detail.textContent = "Overall score: " + (scores.overall_score !== undefined ? scores.overall_score : "n/a") +
        " - see it in Execution History (venture_id: " + job.venture_id + ").";
    } else {
      detail.textContent = "venture_id: " + job.venture_id;
    }
  }

  function stopPolling() {
    if (submitState.pollTimer) {
      clearInterval(submitState.pollTimer);
      submitState.pollTimer = null;
    }
  }

  function refreshJobStatus() {
    if (!submitState.jobId) return;
    fetchJSON(API_BASE + "/submissions/" + submitState.jobId).then(function (job) {
      renderJobStatus(job);
      if (job.status === "done" || job.status === "error") {
        stopPolling();
        document.getElementById("submit-idea-btn").disabled = false;
        notify(job.status === "done" ? "Idea finished processing" : "Idea processing failed", job.status === "done" ? "success" : "error");
        loadHistory(); loadEvents();
      }
    }).catch(function (err) {
      stopPolling();
      document.getElementById("submit-idea-btn").disabled = false;
      notify("Lost track of submission: " + err.message, "error");
    });
  }

  function pollJob(jobId) {
    stopPolling();
    submitState.jobId = jobId;
    refreshJobStatus();
    submitState.pollTimer = setInterval(refreshJobStatus, 3000);
  }

  function submitIdea() {
    var idea = document.getElementById("submit-idea-input").value.trim();
    if (!idea) {
      notify("Enter an idea before submitting", "warning");
      return;
    }
    var ventureId = document.getElementById("submit-venture-id").value.trim();
    var researchDepth = document.getElementById("submit-research-depth").value;
    var btn = document.getElementById("submit-idea-btn");
    btn.disabled = true;

    postJSON(API_BASE + "/submissions", {
      idea: idea, venture_id: ventureId || undefined, research_depth: researchDepth,
    }).then(function (data) {
      notify("Idea submitted - tracking job " + data.job_id, "success");
      pollJob(data.job_id);
    }).catch(function (err) {
      btn.disabled = false;
      notify("Failed to submit idea: " + err.message, "error");
    });
  }

  function setupSubmitForm() {
    document.getElementById("submit-idea-btn").addEventListener("click", submitIdea);
  }

  // ---------------------------------------------------------------- //
  // Global search / filters
  // ---------------------------------------------------------------- //
  function setupFilters() {
    document.getElementById("global-search").addEventListener("input", function (e) {
      var value = e.target.value;
      state.history.search = value; state.history.page = 1;
      state.jobs.search = value; state.jobs.page = 1;
      state.logs.search = value; state.logs.page = 1;
      loadHistory(); loadJobs(); loadLogs();
    });
    document.getElementById("history-search").addEventListener("input", function (e) {
      state.history.search = e.target.value; state.history.page = 1; loadHistory();
    });
    document.getElementById("history-status-filter").addEventListener("change", function (e) {
      state.history.status = e.target.value; state.history.page = 1; loadHistory();
    });
    document.getElementById("jobs-search").addEventListener("input", function (e) {
      state.jobs.search = e.target.value; state.jobs.page = 1; loadJobs();
    });
    document.getElementById("jobs-status-filter").addEventListener("change", function (e) {
      state.jobs.status = e.target.value; state.jobs.page = 1; loadJobs();
    });
    document.getElementById("logs-search").addEventListener("input", function (e) {
      state.logs.search = e.target.value; state.logs.page = 1; loadLogs();
    });
    document.getElementById("logs-category-filter").addEventListener("change", function (e) {
      state.logs.category = e.target.value; state.logs.page = 1; loadLogs();
    });
    document.getElementById("project-selector").addEventListener("change", function (e) {
      state.project = e.target.value;
      loadAll();
    });
    document.getElementById("refresh-btn").addEventListener("click", function () { loadAll(); });
  }

  // ---------------------------------------------------------------- //
  // Orchestration
  // ---------------------------------------------------------------- //
  function loadAll() {
    showLoading(true);
    var ventureParam = { venture_id: state.project };
    return Promise.all([
      fetchJSON(API_BASE + "/metrics", ventureParam),
      fetchJSON(API_BASE + "/health", ventureParam),
      fetchJSON(API_BASE + "/charts", ventureParam),
      loadHistory(),
      loadJobs(),
      loadLogs(),
      loadEvents(),
    ]).then(function (results) {
      renderMetrics(results[0]);
      renderHealth(results[1]);
      renderCharts(results[2]);
      notify("Dashboard updated", "success");
    }).catch(function (err) {
      notify("Failed to load dashboard data: " + err.message, "error");
    }).finally(function () {
      showLoading(false);
    });
  }

  function init() {
    setupCollapsibleCards();
    setupSidebarNav();
    setupFilters();
    setupExportButtons();
    setupSubmitForm();
    loadProjects().finally(loadAll);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.AFOSDashboard = { loadAll: loadAll, state: state };
})();
