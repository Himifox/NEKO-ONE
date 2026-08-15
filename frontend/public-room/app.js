(() => {
  "use strict";

  const state = {
    socket: null,
    reconnectAttempt: 0,
    lastSeq: 0,
    rendered: new Set(),
    heartbeat: null,
    reconnectTimer: null,
    rawStream: "",
    audio: null,
    currentSpeech: null,
    pendingSpeech: null,
    muted: localStorage.getItem("neko.room.soundMuted") === "1",
    connectionState: "connecting",
    controls: { paused: false, read_only: false, proactive_enabled: false },
    stopped: false,
    resyncing: false,
  };

  const timeline = document.getElementById("timeline");
  const composer = document.getElementById("composer");
  const input = document.getElementById("message-input");
  const sendButton = document.getElementById("send-button");
  const connectionState = document.getElementById("connection-state");
  const onlineCount = document.getElementById("online-count");
  const queueState = document.getElementById("queue-state");
  const visitorName = document.getElementById("visitor-name");
  const streamRow = document.getElementById("stream-row");
  const streamText = document.getElementById("stream-text");
  const soundToggle = document.getElementById("sound-toggle");
  const emotionTag = /<\s*(happy|sad|angry|neutral|surprised|surprise|开心|高兴|悲伤|难过|生气|愤怒|惊讶|平静|relaxed)\s*>/gi;
  const emotionAliases = {
    happy: "happy", 开心: "happy", 高兴: "happy",
    sad: "sad", 悲伤: "sad", 难过: "sad",
    angry: "angry", 生气: "angry", 愤怒: "angry",
    surprised: "surprised", surprise: "surprised", 惊讶: "surprised",
    neutral: "neutral", 平静: "neutral", relaxed: "neutral",
  };

  function displayStream(raw) {
    let detected = null;
    const clean = raw.replace(emotionTag, (_, tag) => {
      detected = emotionAliases[String(tag).toLowerCase()] || detected;
      return "";
    });
    streamText.textContent = clean.trimStart();
    if (detected) globalThis.NekoPublicAvatar?.setEmotion?.(detected);
  }

  function updateComposerAvailability() {
    const enabled = state.connectionState === "online"
      && !state.controls.paused
      && !state.controls.read_only;
    input.disabled = !enabled;
    sendButton.disabled = !enabled;
  }

  function setConnection(label, value) {
    connectionState.textContent = label;
    connectionState.dataset.state = value;
    state.connectionState = value;
    updateComposerAvailability();
  }

  function applyRoomControls(controls) {
    state.controls = { ...state.controls, ...(controls || {}) };
    updateComposerAvailability();
    if (state.controls.paused) queueState.textContent = "房间已暂停";
    else if (state.controls.read_only) queueState.textContent = "房间当前只读";
  }

  function updateSoundButton() {
    soundToggle.textContent = state.muted ? "声音：关" : "声音：开";
    soundToggle.setAttribute("aria-pressed", String(state.muted));
  }

  function safeSpeechUrl(value) {
    try {
      const url = new URL(String(value || ""), location.origin);
      if (url.origin !== location.origin || !url.pathname.startsWith("/speech-assets/")) return null;
      return url.href;
    } catch (_) {
      return null;
    }
  }

  function playSharedSpeech(payload) {
    const url = safeSpeechUrl(payload?.url);
    if (!url) {
      state.pendingSpeech = null;
      queueState.textContent = "语音地址无效，已拒绝播放";
      return;
    }
    state.pendingSpeech = payload;
    if (state.muted) return;
    if (state.audio) {
      state.audio.pause();
      globalThis.NekoPublicAvatar?.setSpeaking?.(false);
    }
    const audio = new Audio(url);
    state.audio = audio;
    state.currentSpeech = payload;
    audio.preload = "auto";
    audio.addEventListener("play", () => globalThis.NekoPublicAvatar?.setSpeaking?.(true));
    const stop = () => {
      if (state.audio !== audio) return;
      state.audio = null;
      state.currentSpeech = null;
      globalThis.NekoPublicAvatar?.setSpeaking?.(false);
    };
    audio.addEventListener("ended", () => {
      state.pendingSpeech = null;
      stop();
    }, { once: true });
    audio.addEventListener("error", stop, { once: true });
    audio.play().then(() => {
      if (state.audio === audio) state.pendingSpeech = null;
    }).catch(() => {
      globalThis.NekoPublicAvatar?.setSpeaking?.(false);
      queueState.textContent = "浏览器已阻止自动播放，点击“声音”后重试";
    });
  }

  function updateLastSeq(seq) {
    if (!Number.isInteger(seq) || seq <= state.lastSeq) return;
    state.lastSeq = seq;
  }

  function resetReplay(seq) {
    state.lastSeq = Math.max(0, Number(seq) || 0);
    state.rendered.clear();
    timeline.replaceChildren();
    streamRow.hidden = true;
    state.rawStream = "";
  }

  function requestFullResync() {
    if (state.resyncing || state.stopped) return;
    state.resyncing = true;
    state.lastSeq = 0;
    clearInterval(state.heartbeat);
    queueState.textContent = "检测到时间线缺口，正在重新同步";
    const socket = state.socket;
    if (socket?.readyState === WebSocket.OPEN) {
      socket.close(1012, "sequence_gap");
    } else {
      scheduleReconnect();
    }
  }

  function renderMessage(message) {
    if (!message || state.rendered.has(message.id)) return;
    state.rendered.add(message.id);
    const article = document.createElement("article");
    article.className = "message";
    article.dataset.author = message.author_type === "neko" ? "neko" : "visitor";
    article.dataset.messageId = message.id;

    const header = document.createElement("header");
    const author = document.createElement("strong");
    author.textContent = message.display_name || (message.author_type === "neko" ? "NEKO" : "访客");
    const sequence = document.createElement("span");
    sequence.textContent = `#${message.room_seq}`;
    header.append(author, sequence);

    const body = document.createElement("p");
    body.textContent = message.content || "";
    article.append(header, body);
    timeline.append(article);
    timeline.scrollTop = timeline.scrollHeight;
  }

  function handleEvent(event) {
    if (!event || typeof event.type !== "string") return;
    if (Number.isInteger(event.room_seq) && event.type !== "room.snapshot") {
      if (event.room_seq <= state.lastSeq) return;
      if (event.room_seq !== state.lastSeq + 1) {
        requestFullResync();
        return;
      }
      updateLastSeq(event.room_seq);
    }
    const payload = event.payload || {};
    switch (event.type) {
      case "session.ready":
        visitorName.textContent = event.visitor?.display_name || "游客";
        state.reconnectAttempt = 0;
        state.resyncing = false;
        setConnection("已连接", "online");
        break;
      case "replay.reset":
        resetReplay(payload.replay_from_seq);
        queueState.textContent = payload.reason === "history_expired"
          ? "旧记录已按保留策略清理，时间线已同步"
          : "本地进度已重置，正在同步房间";
        break;
      case "room.snapshot":
        resetReplay(payload.last_room_seq ?? event.room_seq);
        (payload.messages || []).forEach(renderMessage);
        applyRoomControls(payload.controls);
        queueState.textContent = "房间时间线已同步";
        break;
      case "message.created":
        renderMessage(payload);
        break;
      case "message.moderated": {
        const message = timeline.querySelector(`[data-message-id="${CSS.escape(payload.message_id || "")}"]`);
        if (payload.status === "hidden") {
          message?.remove();
          state.rendered.delete(payload.message_id);
        } else if (payload.status === "visible" && payload.message) {
          renderMessage(payload.message);
        }
        break;
      }
      case "presence.updated":
        onlineCount.textContent = String(payload.online ?? 0);
        break;
      case "queue.updated":
        if (state.controls.paused) queueState.textContent = "房间已暂停";
        else if (state.controls.read_only) queueState.textContent = "房间当前只读";
        else {
          queueState.textContent = payload.generating
            ? `NEKO 正在回复 · ${payload.waiting ?? 0} 条等待`
            : payload.waiting
              ? `${payload.waiting} 条消息等待`
              : "等待消息";
        }
        break;
      case "room.control.updated":
        applyRoomControls(payload.controls);
        break;
      case "stream.started":
        state.rawStream = "";
        streamText.textContent = "";
        streamRow.hidden = false;
        break;
      case "stream.delta":
        state.rawStream += payload.delta || "";
        displayStream(state.rawStream);
        break;
      case "stream.snapshot":
        state.rawStream = payload.text || "";
        displayStream(state.rawStream);
        streamRow.hidden = false;
        break;
      case "avatar.state":
        globalThis.NekoPublicAvatar?.setEmotion?.(payload.emotion || "neutral");
        break;
      case "speech.ready":
        playSharedSpeech(payload);
        break;
      case "speech.failed":
        queueState.textContent = "文字回复已完成，语音暂不可用";
        break;
      case "stream.completed":
        streamRow.hidden = true;
        state.rawStream = "";
        streamText.textContent = "";
        break;
      case "stream.failed":
        streamRow.hidden = true;
        state.rawStream = "";
        streamText.textContent = "";
        queueState.textContent = payload.code === "generation_failed"
          ? "回复服务暂时不可用，消息已保留，请稍后再试"
          : "本次回复已中止";
        break;
      case "command.rejected":
        queueState.textContent = `发送失败：${payload.message || payload.code || "未知错误"}`;
        break;
      default:
        break;
    }
  }

  async function establishGuestSession() {
    const response = await fetch("/api/v1/session/guest", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!response.ok) throw new Error(`guest session failed: ${response.status}`);
    const body = await response.json();
    visitorName.textContent = body.visitor.display_name;
    const roomResponse = await fetch("/api/v1/rooms/main", { credentials: "same-origin" });
    if (roomResponse.ok) {
      const room = await roomResponse.json();
      const maximum = Number(room.limits?.max_message_chars || 2000);
      input.maxLength = Math.max(100, Math.min(maximum, 4000));
      applyRoomControls(room.controls);
    }
  }

  function socketUrl() {
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${location.host}/ws/rooms/main?after_seq=${state.lastSeq}`;
  }

  function scheduleReconnect() {
    if (state.stopped) return;
    clearTimeout(state.reconnectTimer);
    const delay = Math.min(15000, 500 * (2 ** state.reconnectAttempt));
    state.reconnectAttempt += 1;
    state.reconnectTimer = setTimeout(connect, delay);
  }

  async function connect() {
    if (state.stopped) return;
    clearInterval(state.heartbeat);
    setConnection("连接中", "connecting");
    try {
      await establishGuestSession();
      const socket = new WebSocket(socketUrl());
      state.socket = socket;
      socket.addEventListener("message", (message) => {
        try { handleEvent(JSON.parse(message.data)); } catch (_) { /* ignore malformed server frame */ }
      });
      socket.addEventListener("open", () => {
        state.heartbeat = setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping" }));
        }, 20000);
      });
      socket.addEventListener("close", () => {
        clearInterval(state.heartbeat);
        if (state.socket !== socket || state.stopped) return;
        state.socket = null;
        setConnection("已断开，正在重连", "offline");
        scheduleReconnect();
      });
      socket.addEventListener("error", () => socket.close());
    } catch (_) {
      setConnection("连接失败，正在重试", "offline");
      scheduleReconnect();
    }
  }

  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || state.socket?.readyState !== WebSocket.OPEN) return;
    const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    state.socket.send(JSON.stringify({
      type: "chat.send",
      request_id: requestId,
      client_time: new Date().toISOString(),
      payload: { text },
    }));
    input.value = "";
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });

  soundToggle.addEventListener("click", () => {
    state.muted = !state.muted;
    localStorage.setItem("neko.room.soundMuted", state.muted ? "1" : "0");
    if (state.muted) {
      if (state.currentSpeech) state.pendingSpeech = state.currentSpeech;
      state.audio?.pause();
      globalThis.NekoPublicAvatar?.setSpeaking?.(false);
    } else if (state.pendingSpeech) {
      playSharedSpeech(state.pendingSpeech);
    }
    updateSoundButton();
  });

  window.addEventListener("pagehide", () => {
    state.stopped = true;
    clearTimeout(state.reconnectTimer);
    clearInterval(state.heartbeat);
    const socket = state.socket;
    state.socket = null;
    socket?.close(1000, "page_unload");
    state.audio?.pause();
    globalThis.NekoPublicAvatar?.setSpeaking?.(false);
  });

  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    state.stopped = false;
    connect();
  });

  updateSoundButton();
  setConnection("连接中", "connecting");
  connect();
})();
