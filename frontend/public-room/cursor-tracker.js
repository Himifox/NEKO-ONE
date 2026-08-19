(() => {
  "use strict";

  const THROTTLE_MS = 200;
  const ZONES = [
    { key: "input_box", selector: "#composer" },
    { key: "avatar_body", selector: "#live2d-canvas" },
    { key: "latest_messages", selector: "#stream-row" },
    { key: "chat_history", selector: "#timeline" },
  ];

  let currentZone = "away";
  let lastSentZone = "away";
  let lastSentTime = 0;
  let boxes = {};

  function queryRect(selector) {
    const el = document.querySelector(selector);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
  }

  function refreshBoxes() {
    for (const zone of ZONES) {
      boxes[zone.key] = queryRect(zone.selector);
    }
  }

  function isInside(rect, x, y) {
    return !!(rect && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom);
  }

  function detectZone(x, y) {
    for (const zone of ZONES) {
      if (isInside(boxes[zone.key], x, y)) return zone.key;
    }
    return "background";
  }

  function sendZone(zone) {
    const send = globalThis.NekoRoomClient?.send;
    if (send) {
      send({ type: "cursor.move", payload: { zone } });
    }
  }

  function handleMove(x, y) {
    const now = Date.now();
    if (now - lastSentTime < THROTTLE_MS) return;

    refreshBoxes();
    const zone = detectZone(x, y);
    currentZone = zone;

    if (zone !== lastSentZone) {
      lastSentZone = zone;
      lastSentTime = now;
      sendZone(zone);
    }
  }

  function handleAway() {
    if (currentZone === "away" && lastSentZone === "away") return;
    currentZone = "away";
    lastSentZone = "away";
    lastSentTime = Date.now();
    sendZone("away");
  }

  refreshBoxes();

  document.addEventListener("mousemove", (e) => {
    handleMove(e.clientX, e.clientY);
  });

  document.addEventListener("mouseleave", handleAway);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) handleAway();
  });

  window.addEventListener("resize", refreshBoxes);
})();