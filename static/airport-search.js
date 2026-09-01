/* ============================================================
   Skairova airport autocomplete — one shared implementation for
   every ".airport-input" on the site (home page, results page,
   agent portal), replacing three independently-drifted copies.
   Backed by /airports, which now calls LiteAPI's own flight-airport
   search instead of the retired local CSV + Duffel places API.
   ============================================================ */
(function () {
  "use strict";

  const DEBOUNCE_MS = 180;
  const MIN_CHARS = 3;
  const PANEL_CLOSE_MS = 180;
  const compactQuery = window.matchMedia("(max-width: 760px)");
  const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

  const isAirportInput = (node) => !!(node && node.classList && node.classList.contains("airport-input"));

  const box = document.createElement("div");
  box.className = "airport-suggest";
  box.id = "airport-suggestions";
  box.setAttribute("role", "listbox");
  box.setAttribute("aria-hidden", "true");

  let activeInput = null;
  let abortCtrl = null;
  let debounceTimer = null;
  let activeIndex = -1;
  let currentItems = [];
  let renderedFor = null;
  let closeTimer = null;
  let pointerDownInBox = false;

  // One selected-display overlay per input, created lazily and reused.
  const overlays = new WeakMap();
  const visibleOverlayInputs = new Set();

  function ready() {
    document.body.appendChild(box);
  }
  if (document.body) ready();
  else document.addEventListener("DOMContentLoaded", ready, { once: true });

  let regionNames = null;
  try { regionNames = new Intl.DisplayNames(["en"], { type: "region" }); } catch (e) { regionNames = null; }
  function countryName(code) {
    const c = String(code || "").trim();
    if (!c) return "";
    if (c.length !== 2 || !regionNames) return c;
    try { return regionNames.of(c.toUpperCase()) || c; } catch (e) { return c; }
  }

  const BUILDING_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="3" width="12" height="18" rx="1"/><path d="M9 21v-4h2v4M8 7h.01M12 7h.01M8 11h.01M12 11h.01M8 15h.01M12 15h.01"/><path d="M16 21V10l4 2v9"/></svg>';

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function itemTitle(item) {
    if (item.subType === "CITY") {
      return `${item.city || item.code} (${item.code} - All airports)`;
    }
    return item.name ? `${item.name} (${item.code})` : item.code;
  }

  function itemSubtitle(item) {
    const country = countryName(item.country);
    if (item.subType === "CITY") return country;
    return [item.city, country].filter(Boolean).join(", ");
  }

  function prepareInput(inp) {
    if (!inp) return;
    inp.setAttribute("role", "combobox");
    inp.setAttribute("aria-autocomplete", "list");
    inp.setAttribute("aria-haspopup", "listbox");
    inp.setAttribute("aria-controls", box.id);
    if (!inp.hasAttribute("aria-expanded")) inp.setAttribute("aria-expanded", "false");
  }

  function updateInputPopupState(inp, open) {
    if (!inp) return;
    prepareInput(inp);
    inp.setAttribute("aria-expanded", String(!!open));
    if (!open) inp.removeAttribute("aria-activedescendant");
  }

  function setBoxOpen(open) {
    if (closeTimer) {
      window.clearTimeout(closeTimer);
      closeTimer = null;
    }
    box.classList.toggle("is-open", !!open);
    box.setAttribute("aria-hidden", String(!open));
    updateInputPopupState(activeInput, open);
  }

  function closeBox(options) {
    const immediate = !!(options && options.immediate);
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    try { abortCtrl?.abort(); } catch (e) { /* ignore */ }
    abortCtrl = null;
    const closingInput = activeInput;
    updateInputPopupState(closingInput, false);
    box.classList.remove("is-open");
    box.setAttribute("aria-hidden", "true");
    activeInput = null;
    activeIndex = -1;
    currentItems = [];
    renderedFor = null;

    if (closeTimer) window.clearTimeout(closeTimer);
    const clear = () => {
      closeTimer = null;
      if (!box.classList.contains("is-open")) box.innerHTML = "";
    };
    if (immediate || reducedMotionQuery.matches) clear();
    else closeTimer = window.setTimeout(clear, PANEL_CLOSE_MS);
  }
  document.addEventListener("airportSuggestClose", closeBox);

  function positionBox(inp) {
    const r = inp.getBoundingClientRect();
    const pad = 12;
    const minSuggest = 360;
    const maxSuggest = 600;
    const visualViewport = window.visualViewport;
    const vw = visualViewport?.width || document.documentElement.clientWidth;
    const vh = visualViewport?.height || window.innerHeight;
    const sx = window.scrollX || window.pageXOffset || 0;
    const avail = Math.max(200, vw - pad * 2);

    let w = Math.max(r.width, minSuggest);
    w = Math.min(w, maxSuggest, avail);

    let left = sx + r.left;
    const maxLeft = sx + vw - pad - w;
    if (left > maxLeft) left = Math.max(sx + pad, maxLeft);

    if (compactQuery.matches) {
      const gap = 8;
      const roomBelow = Math.max(0, vh - r.bottom - gap - pad);
      const roomAbove = Math.max(0, r.top - gap - pad);
      const openAbove = roomBelow < 176 && roomAbove > roomBelow;
      const availableHeight = openAbove ? roomAbove : roomBelow;
      const maxHeight = Math.min(360, Math.max(120, availableHeight));
      const top = openAbove
        ? Math.max(pad, r.top - maxHeight - gap)
        : Math.min(Math.max(pad, r.bottom + gap), Math.max(pad, vh - maxHeight - pad));

      box.classList.toggle("airport-suggest--above", openAbove);
      box.style.left = `${Math.round(left - sx)}px`;
      box.style.top = `${Math.round(top)}px`;
      box.style.width = `${Math.round(w)}px`;
      box.style.maxHeight = `${Math.round(maxHeight)}px`;
      return;
    }

    box.classList.remove("airport-suggest--above");
    box.style.left = `${Math.round(left)}px`;
    box.style.top = `${Math.round((window.scrollY || window.pageYOffset || 0) + r.bottom + 8)}px`;
    box.style.width = `${Math.round(w)}px`;
    box.style.maxHeight = "";
  }

  function setActiveIndex(idx) {
    activeIndex = idx;
    Array.from(box.querySelectorAll(".airport-item")).forEach((el, i) => {
      const active = i === activeIndex;
      el.classList.toggle("active", active);
      el.setAttribute("aria-selected", String(active));
      if (active) {
        activeInput?.setAttribute("aria-activedescendant", el.id);
        try { el.scrollIntoView({ block: "nearest" }); } catch (e) { /* ignore */ }
      }
    });
    if (activeIndex < 0) activeInput?.removeAttribute("aria-activedescendant");
  }

  function render(items) {
    currentItems = items;
    activeIndex = -1;
    box.innerHTML = items.map((it, index) => {
      const isCity = it.subType === "CITY";
      return `
        <button type="button" class="airport-item${isCity ? " airport-item--city" : ""}" id="airport-option-${index}" role="option" aria-selected="false" data-code="${esc(it.code)}">
          ${isCity ? `<span class="airport-item-icon">${BUILDING_ICON}</span>` : ""}
          <span class="airport-item-text">
            <span class="airport-item-title">${esc(itemTitle(it))}</span>
            <span class="airport-item-sub">${esc(itemSubtitle(it))}</span>
          </span>
          ${isCity ? '<span class="airport-item-badge">City</span>' : ""}
        </button>
      `;
    }).join("");
    renderedFor = activeInput;
    setBoxOpen(items.length > 0);
  }

  async function fetchSuggestions(q) {
    if (abortCtrl) abortCtrl.abort();
    abortCtrl = new AbortController();
    const res = await fetch(`/airports?q=${encodeURIComponent(q)}`, {
      signal: abortCtrl.signal,
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data) ? data : [];
  }

  function hintEl(inp) {
    return inp.closest(".airport-field")?.querySelector(".airport-hint") || null;
  }

  function getOverlay(inp) {
    let ov = overlays.get(inp);
    if (ov) return ov;
    ov = document.createElement("div");
    ov.className = "airport-selected-display";
    ov.innerHTML = '<span class="airport-selected-display__city"></span><span class="airport-selected-display__code"></span>';
    ov.addEventListener("click", () => inp.focus());
    document.body.appendChild(ov);
    overlays.set(inp, ov);
    return ov;
  }

  function positionOverlay(inp) {
    const ov = overlays.get(inp);
    if (!ov) return;
    const r = inp.getBoundingClientRect();
    ov.style.left = `${Math.round(window.scrollX + r.left)}px`;
    ov.style.top = `${Math.round(window.scrollY + r.top)}px`;
    ov.style.width = `${Math.round(r.width)}px`;
    ov.style.height = `${Math.round(r.height)}px`;

    // Inherit the real input's own font + padding rather than guessing —
    // different pages give .airport-input wildly different box models (the
    // home page's field draws its padding on an outer pill and leaves the
    // <input> itself at zero padding with 15px/500 text; a standalone
    // bordered input elsewhere carries its own padding directly). Copying
    // the computed values keeps the overlay looking like "this is the real
    // field's value" everywhere instead of one hardcoded guess.
    const cs = inp.ownerDocument.defaultView.getComputedStyle(inp);
    ov.style.font = cs.font;
    ov.style.letterSpacing = cs.letterSpacing;
    ov.style.paddingTop = cs.paddingTop;
    ov.style.paddingBottom = cs.paddingBottom;
    ov.style.paddingLeft = cs.paddingLeft;
    ov.style.paddingRight = cs.paddingRight;
  }

  function hideOverlay(inp) {
    const ov = overlays.get(inp);
    if (ov) ov.style.display = "none";
    visibleOverlayInputs.delete(inp);
  }

  function showOverlaySelection(inp, city, code) {
    const ov = getOverlay(inp);
    ov.querySelector(".airport-selected-display__city").textContent = city || "";
    ov.querySelector(".airport-selected-display__code").textContent = code || "";
    positionOverlay(inp);
    ov.style.display = "flex";
    visibleOverlayInputs.add(inp);
  }

  function commitSelection(inp, item) {
    const code = String(item.code || "").toUpperCase();
    const city = item.city || "";
    const value = city ? `${city} (${code})` : code;
    inp.value = value;
    inp.dispatchEvent(new Event("input", { bubbles: true }));

    const hint = hintEl(inp);
    if (hint) hint.textContent = itemSubtitle(item);

    if (city) showOverlaySelection(inp, city, code);
    else hideOverlay(inp);
  }

  document.addEventListener("input", (event) => {
    const inp = event.target;
    if (!isAirportInput(inp)) return;
    if (activeInput && activeInput !== inp) closeBox({ immediate: true });
    activeInput = inp;
    prepareInput(inp);
    hideOverlay(inp);

    const q = (inp.value || "").trim();
    if (q.length < MIN_CHARS) { closeBox(); return; }

    positionBox(inp);
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(async () => {
      try {
        const items = await fetchSuggestions(q);
        if (document.activeElement !== inp || activeInput !== inp) return;
        render(items);
        positionBox(inp);
      } catch (e) { /* aborted or failed — leave box as-is */ }
    }, DEBOUNCE_MS);
  });

  document.addEventListener("focusin", (event) => {
    const inp = event.target;
    if (!isAirportInput(inp)) return;
    if (activeInput && activeInput !== inp) closeBox({ immediate: true });
    activeInput = inp;
    prepareInput(inp);
    hideOverlay(inp);
    if (box.innerHTML.trim() && currentItems.length && renderedFor === inp) {
      positionBox(inp);
      setBoxOpen(true);
    }
  });

  document.addEventListener("focusout", (event) => {
    const inp = event.target;
    if (!isAirportInput(inp)) return;
    setTimeout(() => {
      if (activeInput !== inp) return;
      if (!pointerDownInBox && !box.contains(document.activeElement)) closeBox();
      const ov = overlays.get(inp);
      const city = ov?.querySelector(".airport-selected-display__city").textContent;
      if (ov && city) {
        positionOverlay(inp);
        ov.style.display = "flex";
        visibleOverlayInputs.add(inp);
      }
    }, 120);
  });

  box.addEventListener("pointerdown", () => {
    pointerDownInBox = true;
  });
  ["pointerup", "pointercancel"].forEach((eventName) => {
    box.addEventListener(eventName, () => {
      // Keep the list alive through the input's delayed blur handler on
      // touch devices; an outside tap will still close it immediately.
      window.setTimeout(() => { pointerDownInBox = false; }, 180);
    });
  });

  box.addEventListener("click", (e) => {
    const btn = e.target.closest(".airport-item");
    if (!btn || !activeInput) return;
    const code = btn.getAttribute("data-code");
    const item = currentItems.find((it) => it.code === code);
    if (!item) return;
    const targetInput = activeInput;
    commitSelection(targetInput, item);
    closeBox();
    targetInput.focus();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && activeInput) { closeBox(); return; }
    const inp = document.activeElement;
    if (!isAirportInput(inp) || !currentItems.length || !box.classList.contains("is-open")) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex(Math.min(activeIndex + 1, currentItems.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex(Math.max(activeIndex - 1, 0));
    } else if (e.key === "Enter" && activeIndex >= 0) {
      e.preventDefault();
      const item = currentItems[activeIndex];
      commitSelection(inp, item);
      closeBox();
    }
  });

  window.addEventListener("scroll", () => {
    if (activeInput) {
      if (activeInput.offsetParent === null) { closeBox(); return; }
      positionBox(activeInput);
    }
    visibleOverlayInputs.forEach((inp) => {
      if (inp.offsetParent === null) hideOverlay(inp);
      else positionOverlay(inp);
    });
  }, { passive: true });

  window.addEventListener("resize", () => {
    if (activeInput) positionBox(activeInput);
    visibleOverlayInputs.forEach(positionOverlay);
  });

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", () => {
      if (activeInput) positionBox(activeInput);
    }, { passive: true });
    window.visualViewport.addEventListener("scroll", () => {
      if (activeInput) positionBox(activeInput);
    }, { passive: true });
  }

  if (typeof compactQuery.addEventListener === "function") {
    compactQuery.addEventListener("change", () => {
      if (activeInput) positionBox(activeInput);
    });
  }

  function isSelectedOverlay(target) {
    for (const inp of visibleOverlayInputs) {
      const overlay = overlays.get(inp);
      if (overlay && overlay.contains(target)) return true;
    }
    return false;
  }

  document.addEventListener("click", (event) => {
    if (box.contains(event.target) || isAirportInput(event.target) || isSelectedOverlay(event.target)) return;
    closeBox();
  });

  window.AirportSearch = {
    init() { /* delegated listeners are already document-wide — nothing to attach per-node */ },
    close: closeBox,
  };
})();
