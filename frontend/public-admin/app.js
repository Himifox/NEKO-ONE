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
    document.getElementById("room-paused").checked = Boolean(state.controls.paused);
    document.getElementById("room-read-only").checked = Boolean(state.controls.read_only);
    document.getElementById("proactive-enabled").checked = Boolean(state.controls.proactive_enabled);
    const cancelGeneration = document.getElementById("cancel-generation");
    cancelGeneration.disabled = !state.active_generation?.cancellable;
    document.getElementById("generation-state").textContent = state.active_generation
      ? `正在生成：${state.active_generation.generation_id} · ${state.active_generation.phase}`
      : "当前没有进行中的回复";
    const dependencyLabels = { llm: "文本模型", memory: "长期记忆", tts: "共享语音" };
    const dependencyState = document.getElementById("dependency-state");
    dependencyState.replaceChildren();
    Object.entries(state.dependencies || {}).forEach(([name, dependency]) => {
      const item = document.createElement("div");
      item.className = "dependency";
      item.dataset.status = dependency.status;
      const label = document.createElement("strong");
      label.textContent = dependencyLabels[name] || name;
      const status = document.createElement("span");
      status.textContent = dependency.status;
      const detail = document.createElement("small");
      detail.textContent = dependency.error_code
        ? `${dependency.error_code} · 连续失败 ${dependency.consecutive_failures}`
        : (dependency.updated_at || "等待首次调用");
      item.append(label, status, detail);
      dependencyState.append(item);
    });
    document.getElementById("message-retention-days").value = state.retention.message_days;
    document.getElementById("visitor-retention-days").value = state.retention.visitor_days;
    document.getElementById("audit-retention-days").value = state.retention.audit_days;
    document.getElementById("speech-retention-hours").value = state.retention.speech_hours;
    document.getElementById("cleanup-interval-minutes").value = state.retention.cleanup_interval_minutes;
    const cleanup = state.last_cleanup;
    document.getElementById("cleanup-state").textContent = cleanup
      ? `上次完成：${cleanup.completed_at} · 删除 ${Object.values(cleanup.counts || {}).reduce((sum, value) => sum + Number(value || 0), 0)} 项 · Memory 失败 ${cleanup.memory_forget_failures || 0}`
      : "尚未执行清理";
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
  document.getElementById("save-controls").addEventListener("click", () => mutate("/room-controls", "PUT", {
    paused: document.getElementById("room-paused").checked,
    read_only: document.getElementById("room-read-only").checked,
    proactive_enabled: document.getElementById("proactive-enabled").checked,
  }));
  document.getElementById("cancel-generation").addEventListener("click", () => mutate("/generation/cancel", "POST"));
  document.getElementById("save-retention").addEventListener("click", () => mutate("/retention", "PUT", {
    message_days: Number(document.getElementById("message-retention-days").value),
    visitor_days: Number(document.getElementById("visitor-retention-days").value),
    audit_days: Number(document.getElementById("audit-retention-days").value),
    speech_hours: Number(document.getElementById("speech-retention-hours").value),
    cleanup_interval_minutes: Number(document.getElementById("cleanup-interval-minutes").value),
  }));
  document.getElementById("run-retention").addEventListener("click", () => {
    if (confirm("立即按当前保留策略永久清理过期数据？")) mutate("/retention/run", "POST");
  });
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
