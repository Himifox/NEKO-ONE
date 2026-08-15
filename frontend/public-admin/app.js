(() => {
  "use strict";
  let csrf = "";
  const loginCard = document.getElementById("login-card");
  const dashboard = document.getElementById("dashboard");
  const notice = document.getElementById("notice");

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { ...(options.headers || {}) };
    if (method !== "GET") headers["X-NEKO-CSRF"] = csrf;
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(`/api/v1/admin${path}`, { ...options, method, headers, credentials: "same-origin" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    return body;
  }

  function button(label, action, id, danger = false) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    element.dataset.action = action;
    element.dataset.id = id;
    if (danger) element.className = "danger";
    return element;
  }

  function row(primary, secondary, actions = []) {
    const element = document.createElement("div"); element.className = "row";
    const strong = document.createElement("strong"); strong.textContent = primary;
    const small = document.createElement("small"); small.textContent = secondary;
    const controls = document.createElement("span"); controls.className = "actions"; controls.append(...actions);
    element.append(strong, small, controls); return element;
  }

  function render(state) {
    document.getElementById("summary").textContent = `${state.online} 在线 · ${state.totals.visitors} 访客 · ${state.totals.messages} 消息`;
    document.getElementById("persona").value = state.persona || "";
    document.getElementById("max-message-chars").value = state.limits.max_message_chars;
    document.getElementById("messages-per-window").value = state.limits.messages_per_window;
    document.getElementById("window-seconds").value = state.limits.window_seconds;
    const visitors = document.getElementById("visitors"); visitors.replaceChildren();
    state.visitors.forEach((visitor) => visitors.append(row(
      `${visitor.display_name} · ${visitor.status}`,
      `${visitor.id} · 最近 ${visitor.last_seen_at}`,
      [
        button(visitor.status === "banned" ? "解封" : "封禁", "visitor-status", visitor.id, visitor.status !== "banned"),
        button("删除独立记忆", "forget", visitor.id, true),
      ],
    )));
    const messages = document.getElementById("messages"); messages.replaceChildren();
    state.messages.forEach((message) => messages.append(row(
      `#${message.room_seq} ${message.display_name} · ${message.status}`,
      message.content,
      [button(message.status === "hidden" ? "恢复" : "隐藏", "message-status", message.id, message.status !== "hidden")],
    )));
    const audit = document.getElementById("audit"); audit.replaceChildren();
    state.audit.forEach((entry) => audit.append(row(entry.action, `${entry.target_type}:${entry.target_id}`, [])));
  }

  async function refresh() { render(await api("/state")); }
  async function mutate(path, method, body) {
    notice.textContent = "处理中…";
    await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
    notice.textContent = "操作完成";
    await refresh();
  }

  document.getElementById("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api("/session", { method: "POST", body: JSON.stringify({ password: document.getElementById("admin-password").value }) });
      csrf = result.csrf; loginCard.hidden = true; dashboard.hidden = false; await refresh();
    } catch (error) { document.getElementById("login-error").textContent = error.message; }
  });
  document.getElementById("refresh").addEventListener("click", refresh);
  document.getElementById("logout").addEventListener("click", async () => { await api("/session", { method: "DELETE" }); location.reload(); });
  document.getElementById("save-persona").addEventListener("click", () => mutate("/persona", "PUT", { system_prompt: document.getElementById("persona").value }));
  document.getElementById("add-fact").addEventListener("click", () => mutate("/memory/room-facts", "POST", { text: document.getElementById("room-fact").value, importance: Number(document.getElementById("fact-importance").value) }));
  document.getElementById("save-limits").addEventListener("click", () => mutate("/limits", "PUT", {
    max_message_chars: Number(document.getElementById("max-message-chars").value),
    messages_per_window: Number(document.getElementById("messages-per-window").value),
    window_seconds: Number(document.getElementById("window-seconds").value),
  }));
  dashboard.addEventListener("click", async (event) => {
    const target = event.target.closest("button[data-action]"); if (!target) return;
    const id = target.dataset.id;
    if (target.dataset.action === "visitor-status") {
      const banned = target.textContent === "解封"; await mutate(`/visitors/${encodeURIComponent(id)}/status`, "PUT", { status: banned ? "active" : "banned" });
    } else if (target.dataset.action === "message-status") {
      const hidden = target.textContent === "恢复"; await mutate(`/messages/${encodeURIComponent(id)}/status`, "PUT", { status: hidden ? "visible" : "hidden" });
    } else if (target.dataset.action === "forget" && confirm("确定删除该访客的独立记忆？此操作不可恢复。")) {
      await mutate(`/memory/visitors/${encodeURIComponent(id)}`, "DELETE");
    }
  });

  api("/session").then(async (session) => { csrf = session.csrf; loginCard.hidden = true; dashboard.hidden = false; await refresh(); }).catch(() => {});
})();
