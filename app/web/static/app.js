/* MyNoteApp 前端 —— 原生 JS，无依赖、无 CDN */

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try {
    body = await res.json();
  } catch (_) {
    /* 有些响应没有 JSON 体 */
  }
  if (!res.ok) {
    const detail = (body && (body.detail || body.message)) || `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return body;
}

/* ------------------------------------------------------------------ */
/* 首页：提交任务 + 轮询进度                                            */
/* ------------------------------------------------------------------ */

function fmtElapsed(seconds) {
  if (seconds < 60) return `${seconds} 秒`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m} 分 ${s} 秒`;
}

let pollTimer = null;
let startedAt = null;

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function renderJob(job) {
  const panel = $("job-panel");
  panel.hidden = false;

  $("job-stage").textContent = job.stage_label;
  $("job-bar").style.width = `${Math.round(job.progress * 100)}%`;
  $("job-msg").textContent = job.message || "";

  if (startedAt) {
    const secs = Math.floor((Date.now() - startedAt) / 1000);
    $("job-elapsed").textContent = `已等待 ${fmtElapsed(secs)}`;
  }

  const log = $("job-log");
  log.textContent = (job.log || []).join("\n");
  log.scrollTop = log.scrollHeight;

  const done = $("job-done");
  if (job.done) {
    done.hidden = false;
    $("cancel-btn").hidden = true;
    stopPolling();
    $("submit-btn").disabled = false;

    if (job.failed) {
      done.className = "job-done failed";
      done.textContent = `失败：${job.error || "未知错误"}`;
      $("job-elapsed").textContent = "";
    } else if (job.cancelled) {
      done.className = "job-done";
      done.textContent = "已取消。";
      $("job-elapsed").textContent = "";
    } else {
      done.className = "job-done";
      done.innerHTML = "";
      const a = document.createElement("a");
      a.href = `/notes/${job.note_id}`;
      a.textContent = "✅ 笔记已生成，点这里查看 →";
      done.appendChild(a);
      $("job-elapsed").textContent = "";
      loadNotes();
    }
  } else {
    done.hidden = true;
    $("cancel-btn").hidden = false;
  }
}

function startPolling(jobId) {
  stopPolling();
  const tick = async () => {
    try {
      renderJob(await api(`/api/jobs/${jobId}`));
    } catch (err) {
      stopPolling();
      $("job-msg").textContent = `获取进度失败：${err.message}`;
      $("submit-btn").disabled = false;
    }
  };
  tick();
  pollTimer = setInterval(tick, 1000);
}

async function submitUrl(event) {
  event?.preventDefault();
  const input = $("url-input");
  const url = input.value.trim();
  const hint = $("form-hint");
  hint.className = "hint";
  hint.textContent = "";

  if (!url) return;

  $("submit-btn").disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    startedAt = Date.now();
    input.value = "";
    renderJob(job);
    startPolling(job.id);
  } catch (err) {
    hint.className = "hint error";
    hint.textContent = err.message;
    $("submit-btn").disabled = false;
  }
}

async function cancelJob() {
  const stage = $("job-stage");
  if (!stage) return;
  try {
    // 当前任务的 id 从最近一次轮询里拿不到，改用列表里的第一条运行中任务
    const jobs = await api("/api/jobs?limit=20");
    const running = jobs.find((j) => !j.done);
    if (running) await api(`/api/jobs/${running.id}/cancel`, { method: "POST" });
  } catch (err) {
    $("job-msg").textContent = `取消失败：${err.message}`;
  }
}

async function loadNotes() {
  const box = $("notes-list");
  if (!box) return;
  try {
    const notes = await api("/api/notes?limit=50");
    $("notes-count").textContent = notes.length ? `共 ${notes.length} 篇` : "";
    if (!notes.length) {
      box.innerHTML =
        '<p class="muted">还没有笔记。把上面的链接贴进来就能生成第一篇。</p>';
      return;
    }
    box.innerHTML = "";
    for (const n of notes) {
      const a = document.createElement("a");
      a.className = "note-item";
      a.href = `/notes/${n.id}`;

      const t = document.createElement("div");
      t.className = "t";
      t.textContent = n.title || n.bvid;

      const o = document.createElement("div");
      o.className = "o";
      o.textContent = n.one_liner || "（还没生成一句话总结）";

      const m = document.createElement("div");
      m.className = "m";
      for (const text of [n.bvid, n.transcript_label, n.created_at]) {
        const s = document.createElement("span");
        s.className = "pill";
        s.textContent = text;
        m.appendChild(s);
      }

      a.append(t, o, m);
      box.appendChild(a);
    }
  } catch (err) {
    box.innerHTML = `<p class="muted">加载笔记列表失败：${err.message}</p>`;
  }
}

/* ------------------------------------------------------------------ */
/* 笔记页                                                              */
/* ------------------------------------------------------------------ */

function wireNotePage() {
  const copyBtn = $("copy-md");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const id = copyBtn.dataset.noteId;
      try {
        const data = await api(`/api/notes/${id}/markdown`);
        await navigator.clipboard.writeText(data.markdown || "");
        copyBtn.textContent = "已复制 ✓";
        setTimeout(() => (copyBtn.textContent = "复制 Markdown"), 1800);
      } catch (err) {
        copyBtn.textContent = "复制失败";
        setTimeout(() => (copyBtn.textContent = "复制 Markdown"), 1800);
      }
    });
  }

  const delBtn = $("delete-note");
  if (delBtn) {
    delBtn.addEventListener("click", async () => {
      if (!confirm("确定删除这篇笔记？磁盘上的 Markdown 文件也会一起删掉。")) return;
      try {
        await api(`/api/notes/${delBtn.dataset.noteId}`, { method: "DELETE" });
        location.href = "/";
      } catch (err) {
        alert(`删除失败：${err.message}`);
      }
    });
  }
}

/* ------------------------------------------------------------------ */
/* 自检页                                                              */
/* ------------------------------------------------------------------ */

const MARK = { ok: "✅", warn: "⚠️", fail: "❌" };

function renderChecks(data) {
  const out = $("checks-out");
  out.innerHTML = "";

  for (const c of data.checks) {
    const row = document.createElement("div");
    row.className = `check-row ${c.status}`;

    const mk = document.createElement("span");
    mk.className = "mk";
    mk.textContent = MARK[c.status] || "?";

    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = c.name;

    const dt = document.createElement("span");
    dt.className = "dt";
    dt.textContent = c.detail;
    if (c.hint && c.status !== "ok") {
      const hn = document.createElement("div");
      hn.className = "hn";
      hn.textContent = `↳ ${c.hint}`;
      dt.appendChild(hn);
    }

    row.append(mk, nm, dt);
    out.appendChild(row);
  }

  const cfg = data.config || {};
  const summary = document.createElement("p");
  summary.className = "hint";
  summary.textContent =
    `云模型：${cfg.cloud_enabled ? "已启用（字幕会外发）" : "未启用"} · ` +
    `B站登录态：${cfg.bili_sessdata ? "已配置" : "未配置"} · ` +
    `转写模型：${cfg.whisper_model}`;
  out.appendChild(summary);
}

async function runChecks() {
  const out = $("checks-out");
  const full = $("full-check")?.checked;
  out.innerHTML = '<p class="muted">检查中…（联网检查可能要十几秒）</p>';
  try {
    renderChecks(await api(`/api/checks${full ? "?full=1" : ""}`));
  } catch (err) {
    out.innerHTML = `<p class="hint error">检查失败：${err.message}</p>`;
  }
}

async function loadStats() {
  const box = $("data-stats");
  if (!box) return;
  try {
    const s = await api("/api/data/stats");
    const mb = (n) => `${(n / 1024 / 1024).toFixed(1)} MB`;
    box.innerHTML = `
      <div class="stat-grid">
        <div class="stat"><div class="k">笔记</div><div class="v">${s.notes} 篇</div></div>
        <div class="stat"><div class="k">音频</div><div class="v">${mb(s.media_bytes)}</div></div>
        <div class="stat"><div class="k">缓存</div><div class="v">${mb(s.cache_bytes)}</div></div>
        <div class="stat"><div class="k">模型权重</div><div class="v">${mb(s.model_bytes)}</div></div>
      </div>`;
  } catch (err) {
    box.innerHTML = `<p class="muted">统计失败：${err.message}</p>`;
  }
}

function wireChecksPage() {
  $("rerun-check")?.addEventListener("click", runChecks);
  $("full-check")?.addEventListener("change", runChecks);
  $("clear-data")?.addEventListener("click", async () => {
    const typed = prompt(
      '这会删除本机全部数据（笔记、转写缓存、音频、模型），不可撤销。\n\n请输入 DELETE 确认：'
    );
    if (typed !== "DELETE") return;
    try {
      const r = await api("/api/data/clear", {
        method: "POST",
        body: JSON.stringify({ confirm: "DELETE" }),
      });
      alert(`已清除 ${r.cleared} 个文件。`);
      location.reload();
    } catch (err) {
      alert(`清除失败：${err.message}`);
    }
  });
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */

document.addEventListener("DOMContentLoaded", () => {
  const form = $("note-form");
  if (form) {
    form.addEventListener("submit", submitUrl);
    $("cancel-btn")?.addEventListener("click", cancelJob);
    loadNotes();
    // 页面刷新后如果还有任务在跑，自动接上进度
    api("/api/jobs?limit=20")
      .then((jobs) => {
        const running = jobs.find((j) => !j.done);
        if (running) {
          startedAt = Date.now() - (running.progress ? 0 : 0);
          $("submit-btn").disabled = true;
          renderJob(running);
          startPolling(running.id);
        }
      })
      .catch(() => {});
  }

  if ($("note-body")) wireNotePage();

  if ($("checks-out")) {
    runChecks();
    loadStats();
    wireChecksPage();
  }
});
