async function apiGet(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

async function apiPut(path, body) {
  const r = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

async function apiPostForm(path, formData) {
  const r = await fetch(path, { method: "POST", body: formData });
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

async function apiPost(path) {
  const r = await fetch(path, { method: "POST" });
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) e.appendChild(c);
  return e;
}

function badge(status) {
  const map = {
    queued: "secondary",
    submitting: "secondary",
    submitted: "info",
    downloading: "primary",
    completed: "success",
    failed: "danger",
    cancelled: "dark",
  };
  const color = map[status] || "secondary";
  return `<span class="badge text-bg-${color}">${status}</span>`;
}

function progressBar(pct) {
  const cl = pct >= 100 ? "bg-success" : "bg-primary";
  return `
    <div class="progress" style="height: 10px;">
      <div class="progress-bar ${cl}" role="progressbar" style="width: ${pct}%" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"></div>
    </div>
    <div class="small text-muted mt-1">${pct}%</div>
  `;
}

async function refreshDownloads() {
  const data = await apiGet("/api/downloads");
  const items = data.items || [];

  const activeItems = items.filter((d) => !["completed", "failed", "cancelled"].includes(d.status));
  const active = activeItems.length;
  const completed = items.filter((d) => d.status === "completed").length;

  const totalSpeed = activeItems.reduce((acc, d) => acc + (d.current_speed_bps || 0), 0);
  const mbps = totalSpeed > 0 ? (totalSpeed / (1024 * 1024)).toFixed(2) : "0.00";

  document.getElementById("activeCount").textContent = String(active);
  document.getElementById("completedCount").textContent = String(completed);
  const tp = document.getElementById("throughputTotal");
  if (tp) tp.textContent = `${mbps} MB/s`;

  const body = document.getElementById("downloadsBody");
  body.innerHTML = "";

  for (const d of items) {
    if (d.status === "completed") continue;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="text-muted">#${d.id}</td>
      <td class="text-truncate" style="max-width: 340px;" title="${d.filename}">${d.filename}</td>
      <td>${d.source_type}</td>
      <td>${d.category ? `<span class="badge text-bg-info">${d.category}</span>` : "—"}</td>
      <td>${badge(d.status)}${d.error ? `<div class="small text-danger mt-1">${d.error}</div>` : ""}</td>
      <td>${progressBar(d.progress)}</td>
      <td class="small text-muted">${d.current_speed_bps ? (d.current_speed_bps / (1024 * 1024)).toFixed(2) + " MB/s" : "—"}</td>
      <td>
        <div class="btn-group btn-group-sm" role="group">
          <button class="btn btn-outline-danger" data-cancel="${d.id}" ${["completed","failed","cancelled"].includes(d.status) ? "disabled" : ""}>Cancel</button>
          <button class="btn btn-outline-secondary" data-delete="${d.id}">Delete</button>
        </div>
      </td>
    `;
    body.appendChild(tr);
  }

  const cbody = document.getElementById("completedBody");
  cbody.innerHTML = "";
  for (const d of items) {
    if (d.status !== "completed") continue;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="text-muted">#${d.id}</td>
      <td class="text-truncate" style="max-width: 340px;" title="${d.filename}">${d.filename}</td>
      <td>${d.category ? `<span class="badge text-bg-info">${d.category}</span>` : "—"}</td>
      <td class="text-truncate" style="max-width: 420px;" title="${d.local_path || ""}">${d.local_path || ""}</td>
      <td class="text-muted small">${d.updated_at}</td>
    `;
    cbody.appendChild(tr);
  }

  body.querySelectorAll("button[data-cancel]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-cancel");
      await apiPost(`/api/downloads/${id}/cancel`);
      await refreshDownloads();
    });
  });

  body.querySelectorAll("button[data-delete]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-delete");
      await fetch(`/api/downloads/${id}`, { method: "DELETE" });
      await refreshDownloads();
    });
  });
}

async function refreshHealth() {
  const h = await apiGet("/api/health");
  document.getElementById("workerStatus").textContent = h.worker_running ? "Running" : "Stopped";
}

async function loadSettings() {
  const s = await apiGet("/api/settings");
  for (const [k, v] of Object.entries(s)) {
    const inp = document.querySelector(`[name="${k}"]`);
    if (!inp) continue;
    if (inp.type === "checkbox") {
      inp.checked = String(v || "").toLowerCase() === "true" || String(v || "") === "1";
    } else {
      inp.value = v || "";
    }
  }
}

async function wire() {
  document.getElementById("refreshBtn").addEventListener("click", refreshDownloads);

  document.getElementById("uploadForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const hint = document.getElementById("uploadHint");
    hint.textContent = "Uploading...";
    try {
      const fd = new FormData(e.target);
      const res = await apiPostForm("/api/downloads/upload", fd);
      hint.textContent = `Queued download #${res.id}.`;
      await refreshDownloads();
      const tabEl = document.querySelector('#downloads-tab');
      bootstrap.Tab.getOrCreateInstance(tabEl).show();
    } catch (err) {
      hint.textContent = String(err);
    }
  });

  document.getElementById("settingsTorboxForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const hint = document.getElementById("settingsHint");
    hint.textContent = "Saving...";
    try {
      const fd = new FormData(e.target);
      const body = Object.fromEntries(fd.entries());
      // include checkbox values explicitly
      const delCb = document.getElementById("deleteOnCompleteProvider");
      if (delCb) body["delete_on_complete_provider"] = delCb.checked ? "true" : "false";
      const bhCb = document.getElementById("blackholeEnabled");
      if (bhCb) body["blackhole_enabled"] = bhCb.checked ? "true" : "false";
      await apiPut("/api/settings", body);
      hint.textContent = "Saved. Restart container to apply rate-limit/concurrency changes to the worker.";
    } catch (err) {
      hint.textContent = String(err);
    }
  });

  document.getElementById("settingsArrForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const hint = document.getElementById("arrHint");
    hint.textContent = "Saving...";
    try {
      const fd = new FormData(e.target);
      const body = Object.fromEntries(fd.entries());
      await apiPut("/api/settings", body);
      hint.textContent = "Saved.";
    } catch (err) {
      hint.textContent = String(err);
    }
  });

  await refreshHealth();
  await loadSettings();
  await refreshDownloads();

  setInterval(refreshHealth, 5000);
  setInterval(refreshDownloads, 2500);
}

wire();

