(() => {
  "use strict";

  const periods = new Set(["morning", "noon", "afternoon", "evening", "late-night"]);
  const labels = {
    morning: "早晨 · 清醒时分",
    noon: "中午 · 明亮日光",
    afternoon: "下午 · 金色片刻",
    evening: "晚上 · 暖灯相伴",
    "late-night": "深夜 · 月色低语",
  };

  function updateLabel(period) {
    const label = document.getElementById("room-period-label");
    if (label) label.textContent = labels[period] || labels.evening;
  }

  function periodForHour(hour) {
    if (hour >= 5 && hour < 11) return "morning";
    if (hour >= 11 && hour < 14) return "noon";
    if (hour >= 14 && hour < 18) return "afternoon";
    if (hour >= 18 && hour < 23) return "evening";
    return "late-night";
  }

  const requested = new URLSearchParams(location.search).get("room-theme");
  const automatic = !periods.has(requested);

  function apply(period) {
    const safePeriod = periods.has(period) ? period : periodForHour(new Date().getHours());
    document.documentElement.dataset.roomPeriod = safePeriod;
    updateLabel(safePeriod);
    return safePeriod;
  }

  let current = apply(automatic ? periodForHour(new Date().getHours()) : requested);
  const timer = automatic
    ? window.setInterval(() => {
        current = apply(periodForHour(new Date().getHours()));
      }, 60 * 1000)
    : null;

  globalThis.NekoRoomTheme = {
    apply,
    periodForHour,
    get current() { return current; },
    get automatic() { return automatic; },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => updateLabel(current), { once: true });
  } else {
    updateLabel(current);
  }

  window.addEventListener("pagehide", (event) => {
    if (!event.persisted && timer !== null) window.clearInterval(timer);
  });
})();
