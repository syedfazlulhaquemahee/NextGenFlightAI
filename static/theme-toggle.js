(function () {
  var KEY = "ngf-theme";
  // Keep the legacy theme implementation in place, but lock the active site
  // to light mode until dark mode is intentionally re-enabled.
  var THEME_TOGGLE_ENABLED = false;
  var meta;

  function getMeta() {
    if (!meta) {
      meta = document.querySelector('meta[name="theme-color"]');
      if (!meta) {
        meta = document.createElement("meta");
        meta.setAttribute("name", "theme-color");
        document.head.appendChild(meta);
      }
    }
    return meta;
  }

  function setChromeColor(isLight) {
    var m = getMeta();
    if (!m) return;
    m.setAttribute("content", isLight ? "#f1f5f9" : "#06121a");
  }

  function applyFromStorage() {
    try {
      if (!THEME_TOGGLE_ENABLED || localStorage.getItem(KEY) === "light") {
        document.documentElement.setAttribute("data-theme", "light");
      } else {
        document.documentElement.removeAttribute("data-theme");
      }
    } catch (e) {}
    setChromeColor(document.documentElement.getAttribute("data-theme") === "light");
  }

  function syncControl() {
    var btn = document.getElementById("themeToggle");
    if (!btn) return;
    var isLight = document.documentElement.getAttribute("data-theme") === "light";
    btn.setAttribute("aria-checked", isLight ? "true" : "false");
    btn.setAttribute("title", isLight ? "Switch to dark theme" : "Switch to light theme");
    setChromeColor(isLight);
    try {
      document.dispatchEvent(new CustomEvent("ngf-themechange", { detail: { theme: isLight ? "light" : "dark" } }));
    } catch (e) {}
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyFromStorage();
    var btn = document.getElementById("themeToggle");
    if (btn && !THEME_TOGGLE_ENABLED) {
      btn.hidden = true;
      btn.disabled = true;
    }
    if (btn && THEME_TOGGLE_ENABLED) {
      btn.addEventListener("click", function () {
        var isLight = document.documentElement.getAttribute("data-theme") === "light";
        try {
          if (isLight) {
            document.documentElement.removeAttribute("data-theme");
            localStorage.removeItem(KEY);
          } else {
            document.documentElement.setAttribute("data-theme", "light");
            localStorage.setItem(KEY, "light");
          }
        } catch (e) {}
        syncControl();
      });
    }
    syncControl();
  });
})();
