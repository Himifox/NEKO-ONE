(() => {
  "use strict";

  const periods = new Set(["morning", "noon", "afternoon", "evening", "late-night"]);

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

  window.addEventListener("pagehide", (event) => {
    if (!event.persisted && timer !== null) window.clearInterval(timer);
  });
})();
