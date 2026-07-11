(function () {
  "use strict";

  var API_BASE = "/api";
  var state = { project: "" };

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

  function el(html) {
    var wrap = document.createElement("div");
    wrap.innerHTML = html.trim();
    return wrap.firstChild;
  }

  function escapeHtml(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
    if (!items.length) {
      wrap.innerHTML = '<div class="empty-state">Nothing here yet.</div>';
      return;
    }
    var head = "<tr>" + columns.map(function (c) { return "<th>" + escapeHtml(c.label) + "</th>"; }).join("") + "</tr>";
    var rows = items.map(function (item) {
      return "<tr>" + columns.map(function (c) { return "<td>" + c.render(item) + "</td>"; }).join("") + "</tr>";
    }).join("");
    wrap.innerHTML = '<table class="data-table"><thead>' + head + "</thead><tbody>" + rows + "</tbody></table>";
  }

  function badge(value) {
    return '<span class="badge ' + escapeHtml(value) + '">' + escapeHtml(value) + "</span>";
  }

  function loadQueue() {
    return fetchJSON(API_BASE + "/approvals", {
      venture_id: state.project,
      search: document.getElementById("queue-search").value,
      status: document.getElementById("queue-status-filter").value,
      stage: document.getElementById("queue-stage-filter").value,
    }).then(function (data) {
      renderTable("queue-table-wrap", data.items, [
        { label: "ID", render: function (r) { return escapeHtml(r.approval_id.slice(0, 8)); } },
        { label: "Stage", render: function (r) { return escapeHtml(r.stage); } },
        { label: "Venture", render: function (r) { return escapeHtml(r.venture_id); } },
        { label: "Status", render: function (r) { return badge(r.status); } },
        { label: "Policy", render: function (r) { return escapeHtml(r.policy); } },
        { label: "Step", render: function (r) { return r.current_approval_step + "/" + r.required_steps; } },
      ]);
    });
  }

  function loadPending() {
    return fetchJSON(API_BASE + "/approvals/pending", { venture_id: state.project }).then(function (data) {
      renderTable("pending-table-wrap", data.items, [
        { label: "ID", render: function (r) { return escapeHtml(r.approval_id.slice(0, 8)); } },
        { label: "Stage", render: function (r) { return escapeHtml(r.stage); } },
        { label: "Reviewers", render: function (r) { return escapeHtml((r.assigned_reviewers || []).join(", ")); } },
        { label: "Requested", render: function (r) { return new Date(r.created_at * 1000).toLocaleString(); } },
      ]);
    });
  }

  function loadDecisions() {
    return fetchJSON(API_BASE + "/approvals/recent_decisions", { venture_id: state.project }).then(function (data) {
      renderTable("decisions-table-wrap", data.items, [
        { label: "ID", render: function (r) { return escapeHtml(r.approval_id.slice(0, 8)); } },
        { label: "Stage", render: function (r) { return escapeHtml(r.stage); } },
        { label: "Status", render: function (r) { return badge(r.status); } },
        { label: "Resolved", render: function (r) { return r.resolved_at ? new Date(r.resolved_at * 1000).toLocaleString() : ""; } },
      ]);
    });
  }

  function loadReviewers() {
    return fetchJSON(API_BASE + "/approvals/reviewer_activity", { venture_id: state.project }).then(function (data) {
      renderTable("reviewers-table-wrap", data.items, [
        { label: "Reviewer", render: function (r) { return escapeHtml(r.reviewer); } },
        { label: "Action", render: function (r) { return escapeHtml(r.action); } },
        { label: "Stage", render: function (r) { return escapeHtml(r.stage); } },
        { label: "When", render: function (r) { return new Date(r.timestamp * 1000).toLocaleString(); } },
      ]);
    });
  }

  function renderList(containerId, items, renderItem) {
    var wrap = document.getElementById(containerId);
    if (!items.length) { wrap.innerHTML = '<div class="empty-state">Nothing here yet.</div>'; return; }
    wrap.innerHTML = items.map(renderItem).join("");
  }

  function loadTimeline() {
    return fetchJSON(API_BASE + "/approvals/timeline", { venture_id: state.project }).then(function (data) {
      renderList("timeline-list-wrap", data.items, function (e) {
        return '<div class="timeline-item">' + escapeHtml(e.action) + " on " + escapeHtml(e.target_type) +
          '<div class="meta">' + escapeHtml(e.actor_id) + " &middot; " + new Date(e.timestamp * 1000).toLocaleString() + "</div></div>";
      });
    });
  }

  function loadFeed() {
    return fetchJSON(API_BASE + "/collaboration/feed", { venture_id: state.project }).then(function (data) {
      renderList("feed-list-wrap", data.items, function (e) {
        return '<div class="feed-item">' + escapeHtml(e.action) + " &rarr; " + escapeHtml(e.target_type) +
          '<div class="meta">' + escapeHtml(e.actor_id) + " &middot; " + new Date(e.timestamp * 1000).toLocaleString() + "</div></div>";
      });
    });
  }

  function loadComments() {
    return fetchJSON(API_BASE + "/collaboration/comments", { venture_id: state.project, search: document.getElementById("comments-search").value }).then(function (data) {
      renderList("comments-list-wrap", data.items, function (c) {
        return '<div class="comment-item">' + escapeHtml(c.body) +
          '<div class="meta">' + escapeHtml(c.author_id) + " on " + escapeHtml(c.target_type) + " &middot; " + new Date(c.created_at * 1000).toLocaleString() + "</div></div>";
      });
    });
  }

  function loadTasks() {
    return fetchJSON(API_BASE + "/collaboration/tasks", { venture_id: state.project, status: document.getElementById("tasks-status-filter").value }).then(function (data) {
      renderTable("tasks-table-wrap", data.items, [
        { label: "Title", render: function (t) { return escapeHtml(t.title); } },
        { label: "Assignee", render: function (t) { return escapeHtml(t.assignee_id || "unassigned"); } },
        { label: "Status", render: function (t) { return badge(t.status); } },
        { label: "Created", render: function (t) { return new Date(t.created_at * 1000).toLocaleString(); } },
      ]);
    });
  }

  function loadTeam() {
    return fetchJSON(API_BASE + "/collaboration/team").then(function (data) {
      renderTable("team-table-wrap", data.members, [
        { label: "Name", render: function (m) { return escapeHtml(m.name); } },
        { label: "Role", render: function (m) { return escapeHtml(m.role); } },
        { label: "Email", render: function (m) { return escapeHtml(m.email); } },
      ]);
    });
  }

  function loadNotifBadge() {
    return fetchJSON(API_BASE + "/notifications", { unread_only: "true" }).then(function (data) {
      document.getElementById("notif-badge").textContent = String(data.total);
    });
  }

  function loadAll() {
    showLoading(true);
    return Promise.all([loadQueue(), loadPending(), loadDecisions(), loadReviewers(), loadTimeline(), loadFeed(), loadComments(), loadTasks(), loadTeam(), loadNotifBadge()])
      .then(function () { notify("Approvals dashboard updated", "success"); })
      .catch(function (err) { notify("Failed to load: " + err.message, "error"); })
      .finally(function () { showLoading(false); });
  }

  function setupFilters() {
    ["queue-search", "queue-status-filter", "queue-stage-filter"].forEach(function (id) {
      document.getElementById(id).addEventListener("input", loadQueue);
      document.getElementById(id).addEventListener("change", loadQueue);
    });
    document.getElementById("comments-search").addEventListener("input", loadComments);
    document.getElementById("tasks-status-filter").addEventListener("change", loadTasks);
    document.getElementById("refresh-btn").addEventListener("click", loadAll);
    document.getElementById("project-selector").addEventListener("change", function (e) {
      state.project = e.target.value;
      loadAll();
    });
  }

  function init() {
    setupCollapsibleCards();
    setupSidebarNav();
    setupFilters();
    loadAll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.AFOSApprovals = { loadAll: loadAll, state: state };
})();
