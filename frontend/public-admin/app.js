(() => {
  "use strict";
  let csrf = "";
  let mutationInFlight = false;
  let avatarModels = [];
  const loginCard = document.getElementById("login-card");
  const dashboard = document.getElementById("dashboard");
  const notice = document.getElementById("notice");
  const loginError = document.getElementById("login-error");
  const adminPassword = document.getElementById("admin-password");

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  function responseError(detail, fallback) {
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const messages = detail.map((item) => item?.msg).filter(Boolean);
      if (messages.length) return messages.join("；");
    }
    return fallback;
  }

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { ...(options.headers || {}) };
    if (method !== "GET") headers["X-NEKO-CSRF"] = csrf;
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(`/api/v1/admin${path}`, { ...options, method, headers, credentials: "same-origin" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new ApiError(
        responseError(body.detail, `HTTP ${response.status}`),
        response.status,
      );
    }
    return body;
  }

  function setNotice(message, state = "idle") {
    notice.textContent = message;
    notice.dataset.state = state;
  }

  function requireLogin(message = "管理会话已失效，请重新登录") {
    csrf = "";
    mutationInFlight = false;
    dashboard.removeAttribute("aria-busy");
    dashboard.hidden = true;
    loginCard.hidden = false;
    loginError.textContent = message;
    adminPassword.focus();
  }

  function reportError(error, prefix = "操作失败") {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      requireLogin();
      return;
    }
    setNotice(`${prefix}：${error?.message || "未知错误"}`, "error");
  }

  function button(label, action, id, danger = false) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    element.dataset.action = action;
    element.dataset.id = id;
    element.className = danger ? "danger small-button" : "secondary small-button";
    return element;
  }

  function row(primary, secondary, actions = []) {
    const element = document.createElement("div"); element.className = "row";
    const copy = document.createElement("span"); copy.className = "row-copy";
    const strong = document.createElement("strong"); strong.textContent = primary;
    const small = document.createElement("small"); small.textContent = secondary;
    const controls = document.createElement("span"); controls.className = "actions"; controls.append(...actions);
    copy.append(strong, small);
    element.append(copy, controls); return element;
  }

  function renderEmpty(container, message) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = message;
    container.append(empty);
  }

  function render(state) {
    document.getElementById("summary").textContent = `${state.online} 在线 · ${state.totals.visitors} 访客 · ${state.totals.messages} 消息`;
    document.getElementById("metric-online").textContent = String(state.online ?? 0);
    document.getElementById("metric-visitors").textContent = String(state.totals.visitors ?? 0);
    document.getElementById("metric-messages").textContent = String(state.totals.messages ?? 0);
    document.getElementById("metric-generation").textContent = state.active_generation ? "生成中" : "空闲";
    const characterSelect = document.getElementById("character-name");
    characterSelect.replaceChildren();
    (state.character_options || []).forEach((character) => {
      const option = document.createElement("option");
      option.value = character.id;
      option.textContent = character.label;
      option.selected = character.id === state.active_character;
      characterSelect.append(option);
    });
    document.getElementById("persona").value = state.persona || "";
    const personaSourceMessage = {
      builtin_default: "当前展示的是内置默认人格；保存后会将它作为此角色的自定义人格。",
      custom: "当前展示的是运行时实际使用的自定义人格。",
    };
    document.getElementById("persona-source").textContent = personaSourceMessage[state.persona_source]
      || "当前人格来源未知；展示的是运行时实际使用的内容。";
    document.getElementById("max-message-chars").value = state.limits.max_message_chars;
    document.getElementById("messages-per-window").value = state.limits.messages_per_window;
    document.getElementById("window-seconds").value = state.limits.window_seconds;
    document.getElementById("room-paused").checked = Boolean(state.controls.paused);
    document.getElementById("room-read-only").checked = Boolean(state.controls.read_only);
    document.getElementById("proactive-enabled").checked = Boolean(state.controls.proactive_enabled);
    const avatar = state.avatar || { current: {}, models: [], management_available: false };
    const currentAvatar = avatar.current || {};
    avatarModels = (avatar.models || []).filter((model) => model.valid);
    const invalidAvatarCount = (avatar.models || []).length - avatarModels.length;
    const avatarStatusLabels = {
      ready: "运行中",
      not_configured: "未启用",
      missing_model: "模型缺失",
      invalid_model: "校验失败",
      invalid_configuration: "配置无效",
    };
    const avatarStatus = document.getElementById("avatar-status");
    avatarStatus.textContent = avatarStatusLabels[currentAvatar.status] || "状态未知";
    avatarStatus.dataset.state = currentAvatar.enabled ? "ready" : "disabled";
    document.getElementById("avatar-current-name").textContent = currentAvatar.model_name || "尚未配置";
    document.getElementById("avatar-current-file").textContent = currentAvatar.model_url || "公共房间将以纯文本模式运行";
    globalThis.NekoAdminAvatar?.show(currentAvatar);
    const avatarManagementAvailable = avatar.management_available !== false;
    document.getElementById("avatar-model-count").textContent = avatarManagementAvailable
      ? `${avatarModels.length} 个可用`
      : "等待后端重启";
    const avatarSelect = document.getElementById("avatar-model");
    avatarSelect.replaceChildren();
    avatarModels.forEach((model, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = `${model.model_name} · ${model.model_file}${model.active ? "（当前）" : ""}`;
      option.selected = Boolean(model.active);
      avatarSelect.append(option);
    });
    if (!avatarModels.length) {
      const option = document.createElement("option");
      option.textContent = avatarManagementAvailable
        ? "没有校验通过的模型"
        : "重启 NEKO 后端后可选择";
      option.value = "";
      avatarSelect.append(option);
    }
    avatarSelect.disabled = !avatarModels.length;
    document.getElementById("save-avatar").disabled = !avatarModels.length;
    document.getElementById("disable-avatar").disabled = !currentAvatar.enabled;
    document.getElementById("avatar-library-state").textContent = !avatarManagementAvailable
      ? "当前仍是旧版后端：已识别正在使用的模型，但切换功能需要重启本地 NEKO 服务后生效。"
      : invalidAvatarCount
        ? `${invalidAvatarCount} 个模型描述文件未通过安全或完整性校验，已禁止选择。`
        : "只显示本机数据目录内通过完整性校验的模型。";
    const cancelGeneration = document.getElementById("cancel-generation");
    cancelGeneration.disabled = !state.active_generation?.cancellable;
    document.getElementById("generation-state").textContent = state.active_generation
      ? `正在生成：${state.active_generation.generation_id} · ${state.active_generation.phase}`
      : "当前没有进行中的回复";
    const dependencyLabels = { llm: "文本模型", memory: "长期记忆", tts: "共享语音" };
    const dependencyStatusLabels = {
      ready: "正常",
      degraded: "降级",
      failed: "故障",
      unknown: "等待调用",
      disabled: "未启用",
    };
    const dependencyState = document.getElementById("dependency-state");
    dependencyState.replaceChildren();
    Object.entries(state.dependencies || {}).forEach(([name, dependency]) => {
      const item = document.createElement("div");
      item.className = "dependency";
      item.dataset.status = dependency.status;
      const label = document.createElement("strong");
      label.textContent = dependencyLabels[name] || name;
      const status = document.createElement("span");
      status.textContent = dependencyStatusLabels[dependency.status] || dependency.status;
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
    (state.visitors || []).forEach((visitor) => visitors.append(row(
      `${visitor.display_name} · ${visitor.status}`,
      `${visitor.id} · 最近 ${visitor.last_seen_at}`,
      [
        button(visitor.status === "banned" ? "解封" : "封禁", "visitor-status", visitor.id, visitor.status !== "banned"),
        button("删除独立记忆", "forget", visitor.id, true),
      ],
    )));
    if (!visitors.children.length) renderEmpty(visitors, "暂无访客记录");
    const messages = document.getElementById("messages"); messages.replaceChildren();
    (state.messages || []).forEach((message) => messages.append(row(
      `#${message.room_seq} ${message.display_name} · ${message.status}`,
      message.content,
      [button(message.status === "hidden" ? "恢复" : "隐藏", "message-status", message.id, message.status !== "hidden")],
    )));
    if (!messages.children.length) renderEmpty(messages, "暂无消息记录");
    const audit = document.getElementById("audit"); audit.replaceChildren();
    (state.audit || []).forEach((entry) => audit.append(row(entry.action, `${entry.target_type}:${entry.target_id}`, [])));
    if (!audit.children.length) renderEmpty(audit, "暂无审计记录");
  }

  async function refresh({ announce = false } = {}) {
    try {
      const state = await api("/state");
      if (!state.avatar) {
        const response = await fetch("/api/v1/avatar", { credentials: "same-origin" });
        state.avatar = {
          current: response.ok ? await response.json() : {},
          models: [],
          management_available: false,
        };
      }
      render(state);
      if (announce) setNotice("状态已刷新", "success");
      return true;
    } catch (error) {
      reportError(error, "刷新失败");
      return false;
    }
  }

  async function mutate(path, method, body) {
    if (mutationInFlight) return false;
    mutationInFlight = true;
    dashboard.setAttribute("aria-busy", "true");
    setNotice("处理中…", "pending");
    let applied = false;
    try {
      await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
      applied = true;
      render(await api("/state"));
      setNotice("操作完成", "success");
      return true;
    } catch (error) {
      reportError(error, applied ? "操作已提交，但状态刷新失败" : "操作失败");
      return false;
    } finally {
      mutationInFlight = false;
      dashboard.removeAttribute("aria-busy");
    }
  }

  document.getElementById("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector("button[type='submit']");
    submit.disabled = true;
    loginError.textContent = "";
    try {
      const result = await api("/session", { method: "POST", body: JSON.stringify({ password: adminPassword.value }) });
      csrf = result.csrf;
      adminPassword.value = "";
      loginCard.hidden = true;
      dashboard.hidden = false;
      await refresh();
    } catch (error) {
      loginError.textContent = error?.message || "登录失败";
    } finally {
      submit.disabled = false;
    }
  });
  document.getElementById("refresh").addEventListener("click", () => refresh({ announce: true }));
  document.getElementById("logout").addEventListener("click", async () => {
    if (mutationInFlight) return;
    try {
      await api("/session", { method: "DELETE" });
      location.reload();
    } catch (error) {
      reportError(error, "退出失败");
    }
  });
  document.getElementById("save-persona").addEventListener("click", () => mutate("/persona", "PUT", { system_prompt: document.getElementById("persona").value }));
  document.getElementById("save-character").addEventListener("click", () => mutate("/character", "PUT", { character: document.getElementById("character-name").value }));
  document.getElementById("add-fact").addEventListener("click", async () => {
    const fact = document.getElementById("room-fact");
    const saved = await mutate("/memory/room-facts", "POST", { text: fact.value, importance: Number(document.getElementById("fact-importance").value) });
    if (saved) fact.value = "";
  });
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
  document.getElementById("save-avatar").addEventListener("click", () => {
    const selected = avatarModels[Number(document.getElementById("avatar-model").value)];
    if (!selected) return;
    mutate("/avatar", "PUT", {
      enabled: true,
      model_name: selected.model_name,
      model_file: selected.model_file,
    });
  });
  document.getElementById("disable-avatar").addEventListener("click", () => {
    if (confirm("停用公共房间的 Live2D 形象？模型文件不会被删除。")) {
      mutate("/avatar", "PUT", { enabled: false });
    }
  });
  document.getElementById("cancel-generation").addEventListener("click", () => {
    if (confirm("确定取消当前正在生成的回复吗？")) mutate("/generation/cancel", "POST");
  });
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

  api("/session").then(async (session) => {
    csrf = session.csrf;
    loginCard.hidden = true;
    dashboard.hidden = false;
    await refresh();
  }).catch(() => {});
})();
