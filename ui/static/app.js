/* Dayfinch shell behaviour: theme, mobile drawer, entrance sequencing, and the
   count-up used on dashboard metrics. No dependencies and no network access. */
(() => {
  "use strict";

  const html = document.documentElement;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");

  /* --- Theme ------------------------------------------------------------ */
  /* The stored value wins over the OS so a deliberate choice survives a
     reload; base.html already applied it before the first paint. */
  const paintThemeButton = () => {
    const dark = html.dataset.theme === "dark";
    document.getElementById("themeColor")?.setAttribute("content", dark ? "#0a0a0d" : "#f6f6f9");
    document.querySelectorAll("[data-theme-icon-light]").forEach((n) => (n.hidden = dark));
    document.querySelectorAll("[data-theme-icon-dark]").forEach((n) => (n.hidden = !dark));
    document
      .querySelectorAll("#themeToggle")
      .forEach((n) => {
        const label = dark ? "Switch to light theme" : "Switch to dark theme";
        n.setAttribute("title", label);
        n.setAttribute("aria-label", label);
      });
  };

  const setTheme = (theme) => {
    html.dataset.theme = theme;
    try {
      localStorage.setItem("df-theme", theme);
    } catch (e) {
      /* Private mode: the theme still applies for this page view. */
    }
    paintThemeButton();
  };

  paintThemeButton();
  document.getElementById("themeToggle")?.addEventListener("click", () => {
    setTheme(html.dataset.theme === "dark" ? "light" : "dark");
  });

  /* --- Mobile drawer ---------------------------------------------------- */
  const sidebar = document.getElementById("sidebar");
  const scrim = document.getElementById("scrim");
  const menuButton = document.getElementById("menuButton");

  if (sidebar && menuButton) {
    const setDrawer = (open) => {
      sidebar.classList.toggle("-translate-x-full", !open);
      menuButton.setAttribute("aria-expanded", String(open));
      if (scrim) scrim.hidden = !open;
      /* Stop the page behind the drawer from scrolling under it. */
      document.body.style.overflow = open ? "hidden" : "";
      if (open) sidebar.querySelector("a")?.focus();
    };

    menuButton.addEventListener("click", () =>
      setDrawer(sidebar.classList.contains("-translate-x-full")),
    );
    scrim?.addEventListener("click", () => setDrawer(false));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !scrim?.hidden) {
        setDrawer(false);
        menuButton.focus();
      }
    });
    /* A resize past the lg breakpoint leaves the drawer state stale. */
    matchMedia("(min-width: 1024px)").addEventListener("change", (event) => {
      if (event.matches) setDrawer(false);
    });
  }

  /* --- Entrance sequencing ---------------------------------------------- */
  /* Number the top-level blocks of the page so `.reveal` staggers them at 95ms
     apart rather than animating everything on the same frame. Anything past the
     first screenful is not worth delaying, so the index is capped. */
  const stagger = document.querySelector("[data-stagger]");
  if (stagger && !reduced.matches) {
    [...stagger.children].forEach((child, index) => {
      child.classList.add("reveal");
      child.style.setProperty("--i", String(Math.min(index, 6)));
    });
    /* Grid tiles inside the first sections get their own quicker sequence. */
    stagger.querySelectorAll("[data-stagger-items] > *").forEach((item, index) => {
      item.classList.add("reveal-fast");
      item.style.setProperty("--i", String(Math.min(index, 10)));
    });
  }

  /* --- Count-up --------------------------------------------------------- */
  /* Metric numbers count to their value once, on first paint. The element's
     text is authoritative: if scripting is off the final number is already
     there, so this only ever animates toward what the server rendered. */
  const countUp = (node) => {
    const target = Number(node.dataset.count);
    if (!Number.isFinite(target)) return;
    const duration = 900;
    const start = performance.now();
    const step = (now) => {
      const progress = Math.min((now - start) / duration, 1);
      /* Expo-out, matching the CSS reveal curve. */
      const eased = progress === 1 ? 1 : 1 - Math.pow(2, -10 * progress);
      node.textContent = Math.round(target * eased).toLocaleString();
      if (progress < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  if (!reduced.matches) document.querySelectorAll("[data-count]").forEach(countUp);

  /* --- Live timer ------------------------------------------------------- */
  /* Tick the running timer forward from the server-rendered seconds so the
     dashboard does not sit on a stale duration between page loads. */
  document.querySelectorAll("[data-elapsed]").forEach((node) => {
    let seconds = Number(node.dataset.elapsed);
    if (!Number.isFinite(seconds)) return;
    const render = () => {
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      const s = Math.floor(seconds % 60);
      node.textContent = `${h}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
    };
    render();
    setInterval(() => {
      seconds += 1;
      render();
    }, 1000);
  });
})();
