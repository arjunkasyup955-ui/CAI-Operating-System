(function () {
  "use strict";

  var API_BASE = "/api/deployment";
  var state = { venture: "", target: "" };

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
    return fetch(path + qs(params || {})).then(function (res) {
      if (!res.ok) {
        return res.json().then(function (err) { throw new Error(err.error || ("HTTP " + res.status)); });
      }
      return res.json();
    });
  }

  function postJSON(path, body) {
    return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function (res) {
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

  function badge(value) {
    return '<span class="badge ' + escapeHtml(value || "unknown") + '">' + escapeHtml(value || "unknown") + "</span>";
  }

  function notify(message, type) {
    var container = document.getElementById("notifications");
    if (!container) return;
    var toast = el('<div class="toast ' + (type || "") + '">' + escapeHtml(message) + "</div>");
    container.appendChild(toast);
    setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 5000);
  }

  function showLoading(visible) {
    var overlay = document.getElementById("loading-overlay");
    if (overlay) overlay.classList.toggle("hidden", !visible);
  }

  function setupCollapsibleCards() {
    document.querySelectorAll(".card-header").forEach(function (header) {
      header.addEventListener("click", function () { header.closest(".card").classList.toggle("collapsed"); });
    });
  }

  function setupSidebarNav() {
    document.querySelectorAll(".nav-link").forEach(function (link) {
      link.addEventListener("click", function () {
        document.querySelectorAll(".nav-link").forEach(function (l) { l.classList.remove("active"); });
        link.classList.add("active");
      });
    });
  }

  function renderTable(containerId, items, columns) {
    var wrap = document.getElementById(containerId);
    if (!items.length) { wrap.innerHTML = '<div class="empty-state">Nothing here yet.</div>'; return; }
    var head = "<tr>" + columns.map(function (c) { return "<th>" + escapeHtml(c.label) + "</th>"; }).join("") + "</tr>";
    var rows = items.map(function (item) {
      return "<tr>" + columns.map(function (c) { return "<td>" + c.render(item) + "</td>"; }).join("") + "</tr>";
    }).join("");
    wrap.innerHTML = '<table class="data-table"><thead>' + head + "</thead><tbody>" + rows + "</tbody></table>";
  }

  function loadStatus() {
    return fetchJSON(API_BASE + "/status", { venture_id: state.venture, target: state.target }).then(function (data) {
      var wrap = document.getElementById("status-table-wrap");
      var rows;
      if (state.target) {
        rows = data.current ? [data.current] : [];
      } else {
        rows = Object.keys(data).map(function (t) { return data[t]; }).filter(Boolean);
      }
      if (!rows.length) { wrap.innerHTML = '<div class="empty-state">No deployments recorded yet.</div>'; return; }
      renderTable("status-table-wrap", rows, [
        { label: "Target", render: function (r) { return escapeHtml(r.target); } },
        { label: "Version", render: function (r) { return escapeHtml(r.version); } },
        { label: "Profile", render: function (r) { return escapeHtml(r.profile); } },
        { label: "Status", render: function (r) { return badge(r.status); } },
        { label: "Deployment ID", render: function (r) { return escapeHtml(r.deployment_id.slice(0, 8)); } },
      ]);
    });
  }

  function loadEnvironment() {
    return fetchJSON(API_BASE + "/environment", { venture_id: state.venture }).then(function (data) {
      var wrap = document.getElementById("environment-wrap");
      var profiles = data.profiles_in_use || [];
      wrap.innerHTML = profiles.length
        ? "<p>Active profiles: " + profiles.map(badge).join(" ") + "</p>"
        : '<div class="empty-state">No environment recorded yet.</div>';
    });
  }

  function loadLatest() {
    return fetchJSON(API_BASE + "/latest", { venture_id: state.venture }).then(function (data) {
      var wrap = document.getElementById("latest-wrap");
      if (!data) { wrap.innerHTML = '<div class="empty-state">No builds yet.</div>'; return; }
      wrap.innerHTML = "<p><strong>" + escapeHtml(data.target) + "</strong> &middot; " + escapeHtml(data.version) +
        " " + badge(data.status) + '<div class="meta">' + new Date(data.created_at * 1000).toLocaleString() + "</div></p>";
    });
  }

  function loadHistory(page) {
    return fetchJSON(API_BASE + "/history", { venture_id: state.venture, target: state.target, page: page || 1, page_size: 10 }).then(function (data) {
      renderTable("history-table-wrap", data.items, [
        { label: "Target", render: function (r) { return escapeHtml(r.target); } },
        { label: "Version", render: function (r) { return escapeHtml(r.version); } },
        { label: "Status", render: function (r) { return badge(r.status); } },
        { label: "ID", render: function (r) { return escapeHtml(r.deployment_id.slice(0, 8)); } },
        { label: "When", render: function (r) { return new Date(r.created_at * 1000).toLocaleString(); } },
      ]);
      var pager = document.getElementById("history-pagination");
      pager.innerHTML = "";
      var prev = el('<button' + (data.page <= 1 ? " disabled" : "") + ">Prev</button>");
      var info = el("<span>Page " + data.page + " of " + Math.max(data.total_pages, 1) + "</span>");
      var next = el('<button' + (data.page >= data.total_pages ? " disabled" : "") + ">Next</button>");
      prev.addEventListener("click", function () { loadHistory(data.page - 1); });
      next.addEventListener("click", function () { loadHistory(data.page + 1); });
      pager.appendChild(prev); pager.appendChild(info); pager.appendChild(next);
    });
  }

  function statusClass(status) {
    return ["healthy", "degraded", "critical"].indexOf(status) >= 0 ? status : "unknown";
  }

  function loadHealth() {
    return fetchJSON(API_BASE + "/health", { venture_id: state.venture }).then(function (data) {
      var grid = document.getElementById("health-grid");
      grid.innerHTML = "";
      var overallTile = el(
        '<div class="health-tile"><div class="label">Overall</div><div class="value"><span class="status-dot ' +
        statusClass(data.overall_health) + '"></span>' + escapeHtml(data.overall_health) + "</div></div>"
      );
      grid.appendChild(overallTile);
      Object.keys(data.per_target || {}).forEach(function (target) {
        var status = data.per_target[target];
        grid.appendChild(el(
          '<div class="health-tile"><div class="label">' + escapeHtml(target) + '</div><div class="value"><span class="status-dot ' +
          statusClass(status) + '"></span>' + escapeHtml(status) + "</div></div>"
        ));
      });
      var dot = document.getElementById("overall-status-dot");
      if (dot) dot.className = "status-dot " + statusClass(data.overall_health);
    });
  }

  function setupRollback() {
    document.getElementById("rollback-btn").addEventListener("click", function () {
      var deploymentId = document.getElementById("rollback-deployment-id").value.trim();
      if (!state.venture || !state.target || !deploymentId) {
        notify("Select a venture and target, and enter a deployment ID", "warning");
        return;
      }
      postJSON(API_BASE + "/rollback", { venture_id: state.venture, target: state.target, deployment_id: deploymentId })
        .then(function (data) {
          notify("Rolled back to " + data.rollback_record.version, "success");
          document.getElementById("rollback-result-wrap").innerHTML =
            "<pre class=\"script-output\">" + escapeHtml(data.rollback_script) + "</pre>";
          loadAll();
        })
        .catch(function (err) { notify("Rollback failed: " + err.message, "error"); });
    });
  }

  function loadAll() {
    showLoading(true);
    return Promise.all([loadStatus(), loadEnvironment(), loadLatest(), loadHistory(1), loadHealth()])
      .then(function () { notify("Deployment dashboard updated", "success"); })
      .catch(function (err) { notify("Failed to load: " + err.message, "error"); })
      .finally(function () { showLoading(false); });
  }

  function setupFilters() {
    document.getElementById("project-selector").addEventListener("change", function (e) { state.venture = e.target.value; loadAll(); });
    document.getElementById("target-selector").addEventListener("change", function (e) { state.target = e.target.value; loadAll(); });
    document.getElementById("refresh-btn").addEventListener("click", loadAll);
  }

  function init() {
    setupCollapsibleCards();
    setupSidebarNav();
    setupFilters();
    setupRollback();
    loadAll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.AFOSDeployment = { loadAll: loadAll, state: state };
})();
