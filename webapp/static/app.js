/* 表格桥接智能体 · 前端逻辑 */
"use strict";

const S = {
  skills: [],
  files: [],
  lastRunParams: null,
  gnFileId: null,
  chatSessionId: null,
  chatBusy: false,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return res;
}
const apiJson = async (path, body) => (await api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {})
})).json();

function mask(show, text) {
  const m = $("mask");
  if (text) $("mask-text").textContent = text;
  m.classList.toggle("show", !!show);
}
function showErr(containerId, msg) {
  $(containerId).innerHTML = msg ? `<div class="err">${esc(msg)}</div>` : "";
}
function roleLabel(role) {
  const map = { leave: "OA 请假单", balance: "HR 年假余额表", overtime: "ERP 加班记录", source: "源表" };
  return map[role] || role;
}

/* ---------------- 页面切换 ---------------- */
document.querySelectorAll(".nav-item").forEach(el => {
  el.onclick = () => {
    document.querySelectorAll(".nav-item").forEach(x => x.classList.remove("active"));
    el.classList.add("active");
    ["workbench", "chat", "generic", "verify", "settings"].forEach(p =>
      $("page-" + p).style.display = p === el.dataset.page ? "" : "none");
    if (el.dataset.page === "settings") loadSettings();
    if (el.dataset.page === "chat") chatWelcome();
  };
});

/* ---------------- 通用表格渲染 ---------------- */
function tableHTML(columns, rows, maxCells = 60) {
  if (!rows || !rows.length) return `<div class="empty">（无数据）</div>`;
  const cols = columns.filter(c => rows.some(r => r[c] !== null && r[c] !== "" && r[c] !== undefined));
  const show = cols.slice(0, maxCells);
  const th = show.map(c => `<th>${esc(c)}</th>`).join("");
  const tr = rows.map(r => `<tr>${show.map(c => `<td>${esc(r[c])}</td>`).join("")}</tr>`).join("");
  return `<div class="tbl-wrap"><table class="data"><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>`;
}
function anomaliesHTML(list) {
  if (!list || !list.length) return `<div class="empty">无异常数据</div>`;
  const rows = list.map(a => ({
    "源表": a["源表"], "行号": a["行号"], "异常类型": a["异常类型"],
    "说明": a["说明"], "原始数据": typeof a["原始数据"] === "object"
      ? Object.entries(a["原始数据"] || {}).filter(([, v]) => v !== "").map(([k, v]) => `${k}=${v}`).join("；")
      : a["原始数据"],
  }));
  return tableHTML(["源表", "行号", "异常类型", "说明", "原始数据"], rows);
}

/* ---------------- 技能 / 角色 ---------------- */
function renderSkillSelect() {
  const sel = $("skill-sel");
  sel.innerHTML = `<option value="">（自动识别意图）</option>` +
    S.skills.map(s => `<option value="${s.key}">${esc(s.name)}</option>`).join("");
  sel.onchange = () => { renderSkillDesc(); renderRoleBox(); };
  renderSkillDesc(); renderRoleBox();
}
function selectedSkill() { return $("skill-sel").value; }
function renderSkillDesc() {
  const s = S.skills.find(x => x.key === selectedSkill());
  $("skill-desc").textContent = s ? s.desc : "根据指令自动选择：年假/请假 → 假期结算；加班/工时 → 加班导入；显式要求通用映射 → 通用映射；其余任意指令 → 自由模式（大模型现场规划，需已配置大模型）";
}
function renderRoleBox() {
  const s = S.skills.find(x => x.key === selectedSkill());
  const box = $("role-box");
  if (!s || !s.sources.length) {
    box.innerHTML = `<label class="f">表角色分配</label><div class="note">该技能无需指定表角色（使用唯一上传的表）。</div>`;
    return;
  }
  box.innerHTML = `<label class="f">表角色分配（已按表头语义自动判断，可改）</label><div class="row">` +
    s.sources.map(src => {
      const opts = S.files.map(f =>
        `<option value="${f.id}" ${f.detected_role === src.role ? "selected" : ""}>${esc(f.name)}</option>`).join("");
      return `<div style="flex:1;min-width:180px"><label class="f" style="margin-top:2px">${esc(roleLabel(src.role))}</label>
        <select data-role="${src.role}" class="role-sel">${opts || "<option value=''>（无可用文件）</option>"}</select></div>`;
    }).join("") + `</div>`;
}
function collectRoleOverride() {
  const out = {};
  document.querySelectorAll(".role-sel").forEach(sel => { if (sel.value) out[sel.dataset.role] = sel.value; });
  return out;
}

/* ---------------- 文件列表 ---------------- */
function renderFiles() {
  const box = $("file-list");
  if (!S.files.length) { box.innerHTML = `<div class="empty">还没有数据。上传导出表，或点「载入内置演示数据」。</div>`; return; }
  box.innerHTML = S.files.map(f => `
    <div class="file-card">
      <b>${esc(f.name)}</b>
      ${f.ocr ? `<span class="badge warn">扫描件 · 建议人工复核</span>` : ""}
      <span class="meta">${f.rows} 行 / ${f.cols} 列${f.anomalies ? `，隔离异常 ${f.anomalies} 条` : ""}</span>
      <span class="role">${f.detected_role ? "→ " + esc(roleLabel(f.detected_role)) + ` (${f.role_score})` : "→ 未识别角色"}</span>
      <span class="x" data-rm="${f.id}" title="移除">&times;</span>
    </div>`).join("");
  box.querySelectorAll("[data-rm]").forEach(el => el.onclick = async () => {
    const d = await apiJson("/api/files/remove", { id: el.dataset.rm });
    S.files = d.files; renderFiles(); renderRoleBox();
  });
  renderRoleBox();
}

async function uploadFiles(input) {
  if (!input.files.length) return;
  mask(true, "解析表格中…");
  try {
    const fd = new FormData();
    [...input.files].forEach(f => fd.append("files", f));
    const pwd = ($("file-password") || {}).value || "";
    if (pwd) fd.append("password", pwd);
    const d = await (await api("/api/files/upload", { method: "POST", body: fd })).json();
    S.files = d.files; renderFiles();
  } catch (ex) { showErr("wb-err", ex.message); }
  finally { mask(false); input.value = ""; }
}

$("file-input").onchange = (e) => uploadFiles(e.target);
$("btn-demo").onclick = async () => {
  mask(true, "载入演示数据…");
  try { S.files = (await apiJson("/api/demo")).files; renderFiles(); showErr("wb-err", ""); }
  catch (ex) { showErr("wb-err", ex.message); } finally { mask(false); }
};
$("btn-clear").onclick = async () => { await apiJson("/api/files/clear"); S.files = []; S.lastRunParams = null; $("result-area").innerHTML = ""; renderFiles(); };

/* ---------------- 示例指令 ---------------- */
const EXAMPLES = [
  "做假期余量结算：先扣年假额度，超出部分自动转成事假，年假和事假分行记录",
  "把生产部 2026年1月到3月 加班时长大于等于2小时的记录，整理成 HR 加班导入模板",
  "结算年假，只看装配车间 2 月的记录",
];
$("examples").innerHTML = EXAMPLES.map(t => `<span class="chip">${esc(t.length > 22 ? t.slice(0, 22) + "…" : t)}</span>`).join("");
$("examples").querySelectorAll(".chip").forEach((el, i) => el.onclick = () => { $("instruction").value = EXAMPLES[i]; });

/* ---------------- 运行 ---------------- */
$("btn-run").onclick = async () => {
  if (!S.files.length) { showErr("wb-err", "请先上传源表或载入演示数据"); return; }
  showErr("wb-err", "");
  mask(true, "智能体执行中…");
  try {
    const params = {
      instruction: $("instruction").value,
      file_ids: S.files.map(f => f.id),
      skill_key: selectedSkill(),
      role_override: collectRoleOverride(),
    };
    const d = await apiJson("/api/run", params);
    S.lastRunParams = params;
    renderResult(d, null, params);
  } catch (ex) { showErr("wb-err", ex.message); }
  finally { mask(false); }
};

function renderResult(d, area, runParams) {
  area = area || $("result-area");
  if (runParams) { area._runParams = runParams; S.lastRunParams = runParams; }
  const anomN = (d.anomalies || []).length;
  area.innerHTML = `
    <div class="card"><div class="row" style="align-items:center">
      <h3 style="margin:0">${esc(d.skill_name)}</h3>
      <span class="badge ok">输出 ${d.total_rows} 行</span>
      <span class="badge ${anomN ? "fail" : "ok"}">异常 ${anomN} 条</span>
      <span style="margin-left:auto" class="row">
        <select id="exp-mode" style="width:auto"><option value="date">日期输出：Excel 日期型</option><option value="text">日期输出：yyyy-mm-dd 文本</option></select>
        <button class="btn primary small" id="btn-export">下载导入文件（含异常日志）</button>
      </span>
    </div>
    <div class="tabs" id="res-tabs">
      <div class="tab active" data-t="trace">执行轨迹</div>
      <div class="tab" data-t="map">字段映射</div>
      <div class="tab" data-t="data">结果数据</div>
      <div class="tab" data-t="detail">计算明细</div>
      <div class="tab" data-t="anom">异常日志${anomN ? `<span class="n">${anomN}</span>` : ""}</div>
    </div>
    <div id="tab-trace"></div><div id="tab-map" style="display:none"></div>
    <div id="tab-data" style="display:none"></div><div id="tab-detail" style="display:none"></div>
    <div id="tab-anom" style="display:none"></div>
  </div>`;

  area.querySelector("#tab-trace").innerHTML = d.trace.map(s =>
    `<div class="step"><b>${esc(s["步骤"])}</b><div class="desc">${esc(s["内容"])}</div><span class="res">→ ${esc(s["结果"])}</span></div>`).join("");

  area.querySelector("#tab-map").innerHTML = Object.entries(d.mapping).map(([role, m]) => `
    <div class="map-role">
      <div class="tname">${esc(roleLabel(role))} ← ${esc(m.table)}</div>
      <div class="map-grid">
        <div class="spec-head">目标字段</div><div class="spec-head">源列</div><div class="spec-head">匹配度</div><div class="spec-head">方式</div>
        ${m.fields.map(f => {
          const opts = [`<option value="">（不使用）</option>`]
            .concat(m.columns.map(c => `<option value="${esc(c)}" ${c === f.source ? "selected" : ""}>${esc(c)}</option>`)).join("");
          return `<div>${esc(f.label)}</div><select data-role="${role}" data-field="${f.key}" class="map-sel">${opts}</select>
            <div class="score">${f.score || "—"}</div><div class="score">${esc(f.method)}</div>`;
        }).join("")}
      </div>
    </div>`).join("") + `<button class="btn small" id="btn-rerun">按修正后的映射重算</button>`;

  area.querySelector("#tab-data").innerHTML =
    `<div class="note">${d.truncated ? `仅预览前 ${d.rows.length} 行（共 ${d.total_rows} 行），导出文件包含全部数据。` : `共 ${d.total_rows} 行`}</div>` +
    tableHTML(d.columns, d.rows);

  area.querySelector("#tab-detail").innerHTML = d.detail && d.detail.settlement
    ? tableHTML(Object.keys(d.detail.settlement[0] || {}), d.detail.settlement) +
      `<div class="note" style="margin-top:10px">处理完成后各员工剩余年假：${esc(JSON.stringify(d.detail.balance_left || {}))}</div>`
    : `<div class="empty">该技能无逐条计算明细。</div>`;

  area.querySelector("#tab-anom").innerHTML = anomaliesHTML(d.anomalies);

  area.querySelectorAll(".tab").forEach(t => t.onclick = () => {
    area.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    ["trace", "map", "data", "detail", "anom"].forEach(k =>
      area.querySelector("#tab-" + k).style.display = k === t.dataset.t ? "" : "none");
  });

  area.querySelector("#btn-rerun").onclick = async () => {
    const override = {};
    area.querySelectorAll(".map-sel").forEach(sel => {
      if (sel.value) (override[sel.dataset.role] = override[sel.dataset.role] || {})[sel.dataset.field] = sel.value;
    });
    const base = area._runParams || S.lastRunParams;
    if (!base) { showErr("wb-err", "原始运行参数已失效，请重新执行"); return; }
    mask(true, "按修正映射重算中…");
    try {
      const d2 = await apiJson("/api/run", { ...base, mapping_override: override });
      S.lastRunParams = { ...base, mapping_override: override };
      renderResult(d2, area, { ...base, mapping_override: override });
      area.querySelector('[data-t="data"]').click();
    } catch (ex) { showErr("wb-err", ex.message); } finally { mask(false); }
  };

  area.querySelector("#btn-export").onclick = async () => {
    mask(true, "生成导入文件中…");
    try {
      const res = await api("/api/export", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: d.run_id, date_mode: area.querySelector("#exp-mode").value }),
      });
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = decodeURIComponent((res.headers.get("content-disposition") || "").split("''")[1] || "导入文件.xlsx");
      a.click(); URL.revokeObjectURL(a.href);
    } catch (ex) { showErr("wb-err", ex.message); } finally { mask(false); }
  };
}

/* ---------------- 对话模式 ---------------- */
function chatWelcome() {
  if ($("chat-msgs").children.length) return;
  const n = S.files.length;
  appendChatMsg("assistant", n
    ? `对话模式已就绪。当前数据源 ${n} 张表：${S.files.map(f => f.name).join("、")}。直接用一句话告诉我要做什么；也可以先问我会怎么处理。`
    : "对话模式已就绪。当前还没有数据表——请先到工作台上传源表或载入演示数据（两边共用数据源），然后在这里用一句话描述任务。");
}

function appendChatMsg(role, text, run) {
  const wrap = document.createElement("div");
  wrap.className = `chat-msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  bubble.innerHTML = String(text).split("\n").map(l => esc(l)).join("<br>");
  wrap.appendChild(bubble);
  if (run) {
    const holder = document.createElement("div");
    holder.className = "chat-result";
    wrap.appendChild(holder);
    renderResult(run, holder, {
      instruction: S._lastChatInstruction || "",
      file_ids: S.files.map(f => f.id),
    });
  }
  $("chat-msgs").appendChild(wrap);
  $("chat-msgs").scrollTop = $("chat-msgs").scrollHeight;
  return wrap;
}

function chatTyping() {
  const wrap = document.createElement("div");
  wrap.className = "chat-msg assistant";
  wrap.innerHTML = `<div class="chat-bubble typing">智能体思考中<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></div>`;
  $("chat-msgs").appendChild(wrap);
  $("chat-msgs").scrollTop = $("chat-msgs").scrollHeight;
  return wrap;
}

async function chatSend() {
  const box = $("chat-input");
  const text = box.value.trim();
  if (!text || S.chatBusy) return;
  box.value = "";
  showErr("chat-err", "");
  appendChatMsg("user", text);
  const typing = chatTyping();
  S.chatBusy = true;
  try {
    const d = await apiJson("/api/chat", { message: text, session_id: S.chatSessionId });
    S.chatSessionId = d.session_id;
    S._lastChatInstruction = d.instruction || "";
    typing.remove();
    appendChatMsg(d.action === "run" && d.run ? "assistant" : "assistant", d.reply, d.action === "run" ? d.run : null);
  } catch (ex) {
    typing.remove();
    appendChatMsg("assistant", "出错了：" + ex.message);
  } finally {
    S.chatBusy = false;
  }
}

$("btn-chat-send").onclick = chatSend;
$("chat-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); chatSend(); }
});
$("btn-chat-clear").onclick = async () => {
  if (S.chatSessionId) { try { await apiJson("/api/chat/reset", { session_id: S.chatSessionId }); } catch (e) { /* ignore */ } }
  S.chatSessionId = null;
  S._lastChatInstruction = null;
  $("chat-msgs").innerHTML = "";
  chatWelcome();
};

/* ---------------- 通用映射页 ---------------- */
function addSpecRow(label = "", source = "", dtype = "text", expr = "") {
  const div = document.createElement("div");
  div.className = "spec-grid";
  const colOpts = S.gnFileId
    ? [`<option value="">（空）</option><option value="__expr__" ${source === "__expr__" || expr ? "selected" : ""}>（写表达式）</option>`]
      .concat((S.gnColumns || []).map(c => `<option value="${esc(c)}" ${c === source ? "selected" : ""}>${esc(c)}</option>`))
    : `<option value="">（先上传源表）</option>`;
  div.innerHTML = `
    <input type="text" class="sp-label" value="${esc(label)}" placeholder="目标列名">
    <select class="sp-src">${colOpts}</select>
    <select class="sp-dtype"><option value="text">文本</option><option value="number">数值</option><option value="date">日期</option></select>
    <input type="text" class="sp-expr" value="${esc(expr)}" placeholder="如 round(时长*8)">
    <span class="x" style="cursor:pointer;text-align:center;color:var(--muted)">&times;</span>`;
  div.querySelector(".sp-dtype").value = dtype;
  if (source && source !== "__expr__") div.querySelector(".sp-src").value = source;
  div.querySelector(".x").onclick = () => div.remove();
  $("spec-list").appendChild(div);
}
$("btn-add-spec").onclick = () => addSpecRow();
addSpecRow("工号"); addSpecRow("姓名"); addSpecRow("部门"); addSpecRow("天数", "", "number");

$("gn-file").onchange = async (e) => uploadGn(e.target);
async function uploadGn(input) {
  if (!input.files.length) return;
  mask(true, "解析表格中…");
  try {
    const fd = new FormData();
    fd.append("files", input.files[0]);
    const d = await (await api("/api/files/upload", { method: "POST", body: fd })).json();
    const f = d.files[d.files.length - 1];
    setGnFile(f);
  } catch (ex) { showErr("gn-err", ex.message); } finally { mask(false); input.value = ""; }
}
async function setGnFile(f) {
  S.gnFileId = f.id;
  const pv = await apiJson("/api/files/preview", { id: f.id, rows: 8 });
  S.gnColumns = pv.columns;
  $("gn-file-info").innerHTML = `已载入：<b>${esc(f.name)}</b>　${f.rows} 行 / ${f.cols} 列${f.anomalies ? `，隔离异常 ${f.anomalies} 条` : ""}`;
  $("gn-preview").innerHTML = tableHTML(pv.columns, pv.rows);
  // 刷新既有下拉
  $("spec-list").innerHTML = "";
  addSpecRow("工号"); addSpecRow("姓名"); addSpecRow("部门"); addSpecRow("天数", "", "number");
}
$("btn-gn-demo").onclick = async () => {
  mask(true, "载入演示数据…");
  try {
    const fd = new FormData();
    const res = await fetch("/api/demo/file?name=OA_请假单导出.xlsx");
    if (!res.ok) throw new Error("演示文件不存在");
    fd.append("files", await res.blob(), "OA_请假单导出.xlsx");
    const d = await (await api("/api/files/upload", { method: "POST", body: fd })).json();
    await setGnFile(d.files[d.files.length - 1]);
    showErr("gn-err", "");
  } catch (ex) { showErr("gn-err", ex.message); } finally { mask(false); }
};

$("btn-gn-run").onclick = async () => {
  if (!S.gnFileId) { showErr("gn-err", "请先上传源报表"); return; }
  showErr("gn-err", "");
  const spec = [...document.querySelectorAll("#spec-list .spec-grid")].map(div => ({
    label: div.querySelector(".sp-label").value.trim(),
    source: div.querySelector(".sp-src").value === "__expr__" ? null : div.querySelector(".sp-src").value || null,
    dtype: div.querySelector(".sp-dtype").value,
    expr: div.querySelector(".sp-src").value === "__expr__" ? div.querySelector(".sp-expr").value.trim() : "",
  })).filter(s => s.label && (s.source || s.expr));
  if (!spec.length) { showErr("gn-err", "请至少配置一个目标列（列名 + 来源列或表达式）"); return; }
  mask(true, "生成导入文件中…");
  try {
    const d = await apiJson("/api/run", {
      instruction: $("gn-cond").value, file_ids: [S.gnFileId],
      skill_key: "generic_map", params: { target_spec: spec },
    });
    $("gn-result").innerHTML = `
      <div class="card">
        <div class="row" style="align-items:center">
          <h3 style="margin:0">生成结果</h3>
          <span class="badge ok">输出 ${d.total_rows} 行</span>
          <span class="badge ${d.anomalies.length ? "fail" : "ok"}">异常 ${d.anomalies.length} 条</span>
          <button class="btn primary small" style="margin-left:auto" id="btn-gn-export">下载导入文件</button>
        </div>
        <div style="margin-top:10px">${tableHTML(d.columns, d.rows)}</div>
        <div style="margin-top:10px">${anomaliesHTML(d.anomalies)}</div>
      </div>`;
    $("btn-gn-export").onclick = () => downloadRun(d.run_id);
  } catch (ex) { showErr("gn-err", ex.message); } finally { mask(false); }
};

async function downloadRun(runId) {
  mask(true, "生成文件中…");
  try {
    const res = await api("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId, date_mode: "date" }),
    });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = decodeURIComponent((res.headers.get("content-disposition") || "").split("''")[1] || "导入文件.xlsx");
    a.click(); URL.revokeObjectURL(a.href);
  } catch (ex) { alert(ex.message); } finally { mask(false); }
}

/* ---------------- 验收自检 ---------------- */
$("btn-verify").onclick = async () => {
  mask(true, "正在执行验收项…");
  try {
    const d = await apiJson("/api/verify", {});
    $("verify-list").innerHTML = d.checks.map(c => `
      <div class="card">
        <div class="row" style="align-items:center">
          <span class="badge ${c["结果"] === "PASS" ? "pass" : "fail"}">${c["结果"]}</span>
          <b>${esc(c["验收项"])}</b>
        </div>
        <div class="note">验收标准：${esc(c["验收标准"])}</div>
        <div style="margin-top:6px;font-size:13px">${esc(c["详情"])}</div>
      </div>`).join("");
  } catch (ex) { $("verify-list").innerHTML = `<div class="err">${esc(ex.message)}</div>`; }
  finally { mask(false); }
};

/* ---------------- 设置 ---------------- */
async function loadSettings() {
  const d = await apiJson2("/api/settings");
  $("set-base").value = d.base_url || "";
  $("set-model").value = d.model || "";
  $("set-key").value = "";
  $("set-key").placeholder = d.api_key_set ? "已保存（留空表示不修改）" : "sk-...";
  $("set-status").innerHTML = d.llm_ready
    ? `<span class="badge ok">大模型已连接</span>`
    : `<span class="badge off">未配置 · 纯规则模式</span>`;
}
async function apiJson2(path) { return (await api(path)).json(); }

$("btn-save-settings").onclick = async () => {
  try {
    const d = await apiJson("/api/settings", {
      base_url: $("set-base").value, api_key: $("set-key").value, model: $("set-model").value,
    });
    $("set-status").innerHTML = d.llm_ready ? `<span class="badge ok">已保存，大模型可用</span>` : `<span class="badge off">已保存 · 当前为纯规则模式（缺少 URL 或 Key）</span>`;
  } catch (ex) { $("set-status").innerHTML = `<span class="badge fail">${esc(ex.message)}</span>`; }
};
$("btn-test-settings").onclick = async () => {
  $("set-status").innerHTML = `测试中…`;
  try {
    const d = await apiJson("/api/settings/test", {
      base_url: $("set-base").value, api_key: $("set-key").value, model: $("set-model").value,
    });
    $("set-status").innerHTML = d.ok ? `<span class="badge ok">${esc(d.message)}</span>` : `<span class="badge fail">${esc(d.message)}</span>`;
  } catch (ex) { $("set-status").innerHTML = `<span class="badge fail">${esc(ex.message)}</span>`; }
};

/* ---------------- 初始化 ---------------- */
(async function init() {
  const d = await apiJson2("/api/skills");
  S.skills = d.skills;
  renderSkillSelect();
  renderFiles();
})();
