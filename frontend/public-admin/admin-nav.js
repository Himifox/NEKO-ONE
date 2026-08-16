(() => {
  "use strict";

  const layout = document.querySelector(".dashboard-layout");
  const sidebar = document.getElementById("admin-sidebar");
  const toggle = document.getElementById("nav-toggle");
  const navLinks = Array.from(document.querySelectorAll(".side-nav a[href^='#']"));
  const sections = Array.from(document.querySelectorAll(".console-section"));
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;

  function setOpen(open) {
    if (!layout || !toggle) return;
    layout.classList.toggle("is-open", open);
    document.body.classList.toggle("nav-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "关闭导航菜单" : "打开导航菜单");
  }

  function activeLink(id) {
    const target = `#${id}`;
    navLinks.forEach((link) => link.classList.toggle("is-active", link.getAttribute("href") === target));
  }

  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = !layout?.classList.contains("is-open");
      setOpen(next);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && layout?.classList.contains("is-open")) {
        setOpen(false);
        toggle?.focus();
      }
    });
    document.addEventListener("click", (event) => {
      if (!layout?.classList.contains("is-open")) return;
      if (event.target.closest("a[href^='#']")) setOpen(false);
    });
    if (reduceMotion) setOpen(false);
  }

  if (sections.length && navLinks.length && "IntersectionObserver" in window) {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((entry) => entry.isIntersecting);
        if (!visible.length) return;
        const section = visible.sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        activeLink(section.target.id);
      },
      { rootMargin: "-15% 0px -45% 0px", threshold: [0, 0.1, 0.25] },
    );
    sections.forEach((section) => observer.observe(section));
  }

  const applyHash = () => {
    const id = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (id && document.getElementById(id)) activeLink(id);
  };
  window.addEventListener("hashchange", applyHash);
  applyHash();
})();
