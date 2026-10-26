// 관리자 화면. 대시보드(chat_logs 집계)와 프롬프트 튜닝(prompts 버전 관리).
// 로그에는 사용자가 쓴 글이 들어 있으므로 항상 textContent 로만 그린다.

const $error = document.getElementById("error");
let prompts = [];
let currentPrompt = null;

function showError(message) {
  $error.textContent = message;
  $error.hidden = !message;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("로그인이 필요합니다.");
  }
  if (!res.ok) {
    throw new Error(body.message || body.detail || `요청 실패 (${res.status})`);
  }
  return body;
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function fillTable(table, headers, rows) {
  table.replaceChildren();
  const head = el("tr");
  headers.forEach((h) => head.appendChild(el("th", h)));
  table.appendChild(head);
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "데이터가 없습니다.", "muted");
    td.colSpan = headers.length;
    tr.appendChild(td);
    table.appendChild(tr);
    return;
  }
  rows.forEach((cells) => {
    const tr = el("tr");
    cells.forEach((cell) => {
      const td = el("td");
      if (cell instanceof Node) td.appendChild(cell);
      else td.textContent = cell ?? "-";
      tr.appendChild(td);
    });
    table.appendChild(tr);
  });
}

const ms = (value) => (value === null || value === undefined ? "-" : `${Number(value).toLocaleString()}ms`);
const time = (value) => new Date(value).toLocaleString("ko-KR", { hour12: false });

// --- 대시보드 ---

async function loadDashboard() {
  const days = document.getElementById("days").value;
  const data = await api(`/api/admin/dashboard?days=${days}`);
  const s = data.summary;
  const errorRate = s.total ? ((s.errors / s.total) * 100).toFixed(1) : "0.0";

  const stats = document.getElementById("stats");
  stats.replaceChildren();
  [
    ["질문 수", Number(s.total).toLocaleString()],
    ["사용자 수", Number(s.users).toLocaleString()],
    ["에러율", `${errorRate}%`],
    ["평균 응답", ms(s.avg_ms)],
    ["p95 응답", ms(s.p95_ms)],
  ].forEach(([label, value]) => {
    const card = el("div", null, "stat");
    card.appendChild(el("div", label, "stat-label"));
    card.appendChild(el("div", value, "stat-value"));
    stats.appendChild(card);
  });

  const total = data.routes.reduce((sum, r) => sum + r.count, 0) || 1;
  fillTable(
    document.getElementById("routes"),
    ["경로", "건수", "비율", "평균", "p95"],
    data.routes.map((r) => {
      const share = el("div", null, "bar");
      const fill = el("span");
      fill.style.width = `${(r.count / total) * 100}%`;
      share.appendChild(fill);
      share.appendChild(el("em", `${((r.count / total) * 100).toFixed(0)}%`));
      return [r.route, r.count, share, ms(r.avg_ms), ms(r.p95_ms)];
    })
  );

  fillTable(
    document.getElementById("daily"),
    ["날짜", "질문 수", "에러"],
    data.daily.map((d) => [d.day, d.count, d.errors])
  );

  fillTable(
    document.getElementById("recent"),
    ["시각", "경로", "상태", "응답", "질문", "답변 / 에러"],
    data.recent.map((r) => [time(r.created_at), r.route, r.status, ms(r.latency_ms), r.question, r.answer])
  );
}

document.getElementById("days").addEventListener("change", () => loadDashboard().catch((e) => showError(e.message)));

document.getElementById("reload-agent").addEventListener("click", async () => {
  const result = document.getElementById("reload-result");
  try {
    const body = await api("/api/admin/reload", { method: "POST" });
    result.textContent = `다시 읽었어요. RAG 문서 ${body.rag_documents.length}개`;
  } catch (e) {
    showError(e.message);
  }
});

// --- 프롬프트 튜닝 ---

async function loadPrompts(selectName) {
  prompts = await api("/api/admin/prompts");
  const list = document.getElementById("prompt-list");
  list.replaceChildren();
  prompts.forEach((p) => {
    const item = el("li");
    const button = el("button", p.name, "prompt-item");
    button.type = "button";
    const active = p.versions.find((v) => v.is_active);
    button.appendChild(el("small", active ? `v${active.version} 적용 중` : "기본값"));
    button.addEventListener("click", () => selectPrompt(p.name));
    item.appendChild(button);
    list.appendChild(item);
  });
  if (selectName || currentPrompt) selectPrompt(selectName || currentPrompt);
}

function selectPrompt(name) {
  const p = prompts.find((item) => item.name === name);
  if (!p) return;
  currentPrompt = name;
  document.querySelectorAll(".prompt-item").forEach((b) => {
    b.setAttribute("aria-current", b.firstChild.textContent === name ? "true" : "false");
  });
  document.getElementById("prompt-editor").hidden = false;
  document.getElementById("prompt-name").textContent = name;
  document.getElementById("prompt-hint").textContent = p.placeholders.length
    ? `반드시 남겨야 하는 자리표시자: ${p.placeholders.map((k) => `{${k}}`).join(", ")} · JSON 예시의 중괄호는 {{ }} 로 씁니다.`
    : "자리표시자가 없는 프롬프트입니다.";
  document.getElementById("prompt-content").value = p.current;
  document.getElementById("prompt-note").value = "";
  document.getElementById("prompt-result").textContent = "";

  fillTable(
    document.getElementById("prompt-versions"),
    ["버전", "상태", "메모", "작성자", "시각", ""],
    p.versions.map((v) => {
      const button = el("button", v.is_active ? "적용 중" : "이 버전 적용", "secondary small");
      button.type = "button";
      button.disabled = v.is_active;
      button.addEventListener("click", () => activate(name, v.version));
      return [`v${v.version}`, v.is_active ? "적용 중" : "", v.note, v.created_by, time(v.created_at), button];
    })
  );
}

async function activate(name, version) {
  try {
    await api(`/api/admin/prompts/${name}/activate`, {
      method: "POST",
      body: JSON.stringify({ version }),
    });
    await loadPrompts(name);
    document.getElementById("prompt-result").textContent =
      version === 0 ? "코드 기본값으로 되돌렸어요." : `v${version}을(를) 적용했어요.`;
  } catch (e) {
    showError(e.message);
  }
}

document.getElementById("prompt-save").addEventListener("click", async () => {
  showError("");
  try {
    const body = await api(`/api/admin/prompts/${currentPrompt}`, {
      method: "POST",
      body: JSON.stringify({
        content: document.getElementById("prompt-content").value,
        note: document.getElementById("prompt-note").value || null,
      }),
    });
    await loadPrompts(currentPrompt);
    document.getElementById("prompt-result").textContent = `v${body.version}으로 저장하고 적용했어요. 다음 질문부터 반영돼요.`;
  } catch (e) {
    showError(e.message);
  }
});

document.getElementById("prompt-reset").addEventListener("click", () => activate(currentPrompt, 0));

// --- 탭 ---

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", t === tab ? "true" : "false"));
    document.getElementById("tab-dashboard").hidden = tab.dataset.tab !== "dashboard";
    document.getElementById("tab-prompts").hidden = tab.dataset.tab !== "prompts";
    showError("");
    const load = tab.dataset.tab === "dashboard" ? loadDashboard() : loadPrompts();
    load.catch((e) => showError(e.message));
  });
});

loadDashboard().catch((e) => showError(e.message));
