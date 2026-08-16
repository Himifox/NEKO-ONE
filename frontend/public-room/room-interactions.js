(() => {
  "use strict";

  const FOLLOW_KEY = "neko.room.followed";
  const SHAPES = ["burst-heart", "burst-star"];

  const follow = document.getElementById("follow-button");
  const like = document.getElementById("like-button");
  const layer = document.getElementById("like-burst-layer");
  const badge = document.getElementById("like-count");

  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
  let likeTotal = 0;

  function renderFollow() {
    if (!follow) return;
    const followed = localStorage.getItem(FOLLOW_KEY) === "1";
    follow.setAttribute("aria-pressed", String(followed));
    follow.classList.toggle("is-followed", followed);
    const heart = follow.querySelector(".follow-heart");
    const label = follow.querySelector(".follow-label");
    if (heart) heart.textContent = followed ? "♥" : "♡";
    if (label) label.textContent = followed ? "已关注" : "关注";
  }

  function spawnBurst() {
    const count = 1 + Math.floor(Math.random() * 3);
    for (let i = 0; i < count; i += 1) {
      const item = document.createElement("span");
      item.className = "burst " + SHAPES[Math.floor(Math.random() * SHAPES.length)];
      item.style.left = `${12 + Math.random() * 66}%`;
      item.style.setProperty("--dx", `${(Math.random() * 160 - 80).toFixed(0)}px`);
      item.style.setProperty("--dur", `${(1.1 + Math.random() * 0.9).toFixed(2)}s`);
      item.style.setProperty("--delay", `${(Math.random() * 0.18).toFixed(2)}s`);
      layer.append(item);
      item.addEventListener("animationend", () => item.remove(), { once: true });
    }
  }

  if (follow) {
    renderFollow();
    follow.addEventListener("click", () => {
      const next = localStorage.getItem(FOLLOW_KEY) !== "1";
      localStorage.setItem(FOLLOW_KEY, next ? "1" : "0");
      renderFollow();
    });
  }

  if (like && layer) {
    like.addEventListener("click", () => {
      likeTotal += 1;
      if (badge) {
        badge.textContent = String(likeTotal);
        badge.hidden = false;
      }
      if (reduceMotion) return;
      spawnBurst();
    });
  }
})();
