/* Flexible-date month selector. The date range picker lives in date-picker.js;
 * this control deliberately supports several candidate travel months. */
(function () {
  const firstMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
  const maxMonths = 6;
  const PANEL_CLOSE_MS = 180;
  const compactQuery = window.matchMedia("(max-width: 760px)");
  const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

  function monthValue(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
  }

  function monthLabel(value, short) {
    if (!/^\d{4}-\d{2}$/.test(value || "")) return "";
    const [year, month] = value.split("-").map(Number);
    return new Intl.DateTimeFormat("en-US", { month: short ? "short" : "long", year: "numeric" }).format(new Date(year, month - 1, 1));
  }

  function choices(count) {
    return Array.from({ length: count }, (_, index) => {
      const date = new Date(firstMonth.getFullYear(), firstMonth.getMonth() + index, 1);
      return { value: monthValue(date), label: monthLabel(monthValue(date), false) };
    });
  }

  function parseMonths(value) {
    return [...new Set(String(value || "").split(",").map((item) => item.trim()).filter((item) => /^\d{4}-\d{2}$/.test(item)))];
  }

  function displayLabel(months) {
    if (!months.length) return "Choose month(s)";
    if (months.length === 1) return monthLabel(months[0], true);
    if (months.length === 2) return months.map((month) => monthLabel(month, true)).join(" + ");
    return `${monthLabel(months[0], true)} + ${months.length - 1} more`;
  }

  function initMonthPicker() {
    const monthInput = document.getElementById("flexMonthPicker");
    const monthsInput = document.getElementById("flexMonthsPicker");
    const trigger = document.getElementById("flexMonthPickerTrigger");
    const display = document.getElementById("flexMonthPickerDisplay");
    if (!monthInput || !monthsInput || !trigger || !display || monthInput.dataset.monthPickerBound) return;
    monthInput.dataset.monthPickerBound = "true";

    const popover = document.createElement("section");
    popover.className = "flex-month-popover nx-month-picker";
    popover.id = "flexMonthPickerPopover";
    popover.hidden = true;
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-label", "Choose travel months");
    popover.setAttribute("aria-hidden", "true");
    popover.setAttribute("tabindex", "-1");
    popover.innerHTML = `
      <div class="flex-month-popover__head"><div><strong>When would you like to travel?</strong><span>Select one or more months to compare prices.</span></div><button type="button" class="flex-month-popover__close" aria-label="Close month picker">×</button></div>
      <div class="flex-month-grid">${choices(18).map(({ value, label }) => `<button type="button" class="flex-month-option" data-month="${value}" aria-pressed="false">${label}</button>`).join("")}</div>
      <div class="flex-month-popover__footer"><span class="flex-month-selection">No months selected</span><div><button type="button" class="flex-month-clear">Clear</button><button type="button" class="flex-month-apply">Done</button></div></div>`;
    const backdrop = document.createElement("div");
    backdrop.className = "flex-month-backdrop";
    backdrop.setAttribute("aria-hidden", "true");
    backdrop.hidden = true;
    document.body.appendChild(backdrop);
    document.body.appendChild(popover);
    trigger.setAttribute("aria-controls", popover.id);

    const options = Array.from(popover.querySelectorAll(".flex-month-option"));
    const summary = popover.querySelector(".flex-month-selection");
    let selected = parseMonths(monthsInput.value || monthInput.value);

    function sync(notify) {
      selected.sort();
      monthInput.value = selected[0] || "";
      monthsInput.value = selected.join(",");
      display.textContent = displayLabel(selected);
      display.classList.toggle("is-placeholder", !selected.length);
      options.forEach((option) => {
        const active = selected.includes(option.dataset.month || "");
        option.classList.toggle("is-selected", active);
        option.setAttribute("aria-pressed", String(active));
      });
      summary.textContent = selected.length ? `${selected.length} month${selected.length === 1 ? "" : "s"} selected` : "No months selected";
      if (notify) [monthInput, monthsInput].forEach((input) => {
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }

    let closeTimer = null;

    function isOpen() {
      return !popover.hidden && popover.classList.contains("is-open");
    }

    function syncPageState() {
      const open = !popover.hidden;
      document.body.classList.toggle("month-roller-open", open);
      document.documentElement.classList.toggle("month-roller-scroll-locked", open && compactQuery.matches);
    }

    function position() {
      if (popover.hidden) return;
      if (compactQuery.matches) {
        // Mobile is laid out as a bottom sheet by date-picker.css.
        popover.style.width = "";
        popover.style.left = "";
        popover.style.top = "";
        return;
      }
      const rect = trigger.getBoundingClientRect();
      const width = Math.min(448, window.innerWidth - 24);
      const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect.left));
      popover.style.width = `${width}px`;
      popover.style.left = `${left + window.scrollX}px`;
      popover.style.top = `${rect.bottom + window.scrollY + 10}px`;
    }

    function finishClose(returnFocus) {
      closeTimer = null;
      popover.hidden = true;
      backdrop.hidden = true;
      popover.classList.remove("is-closing");
      backdrop.classList.remove("is-closing");
      trigger.setAttribute("aria-expanded", "false");
      syncPageState();
      if (returnFocus) {
        try { trigger.focus({ preventScroll: true }); } catch (e) { trigger.focus(); }
      }
    }

    function open(options) {
      options = options || {};
      if (isOpen()) {
        position();
        return;
      }
      if (closeTimer) {
        window.clearTimeout(closeTimer);
        closeTimer = null;
      }
      popover.hidden = false;
      backdrop.hidden = false;
      popover.classList.remove("is-closing");
      backdrop.classList.remove("is-closing");
      popover.setAttribute("aria-hidden", "false");
      if (compactQuery.matches) popover.setAttribute("aria-modal", "true");
      else popover.removeAttribute("aria-modal");
      trigger.setAttribute("aria-expanded", "true");
      syncPageState();
      position();
      window.requestAnimationFrame(() => {
        if (popover.hidden || popover.classList.contains("is-closing")) return;
        popover.classList.add("is-open");
        backdrop.classList.add("is-open");
        if (options.focusFirst) {
          const closeButton = popover.querySelector(".flex-month-popover__close");
          try { closeButton?.focus({ preventScroll: true }); } catch (e) { closeButton?.focus(); }
        }
      });
    }

    function close(options) {
      options = options || {};
      if (popover.hidden) return;
      popover.classList.remove("is-open");
      popover.classList.add("is-closing");
      popover.setAttribute("aria-hidden", "true");
      backdrop.classList.remove("is-open");
      backdrop.classList.add("is-closing");
      if (closeTimer) window.clearTimeout(closeTimer);
      if (!options.immediate && !reducedMotionQuery.matches) {
        closeTimer = window.setTimeout(() => finishClose(!!options.returnFocus), PANEL_CLOSE_MS);
      } else {
        finishClose(!!options.returnFocus);
      }
    }

    monthInput._monthRollerDisplay = trigger;
    monthInput._openMonthRoller = open;
    monthInput._closeMonthRoller = close;
    trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      if (isOpen()) close();
      else open({ focusFirst: event.detail === 0 });
    });
    trigger.addEventListener("keydown", (event) => {
      if (["Enter", " ", "ArrowDown"].includes(event.key)) { event.preventDefault(); open({ focusFirst: true }); }
      if (event.key === "Escape") close({ returnFocus: true });
    });
    popover.addEventListener("click", (event) => {
      const option = event.target.closest(".flex-month-option");
      if (option) {
        const value = option.dataset.month || "";
        selected = selected.includes(value) ? selected.filter((month) => month !== value) : [...selected, value];
        if (selected.length > maxMonths) selected = selected.slice(-maxMonths);
        sync(true);
      } else if (event.target.closest(".flex-month-clear")) { selected = []; sync(true); }
      else if (event.target.closest(".flex-month-apply") || event.target.closest(".flex-month-popover__close")) close();
    });
    backdrop.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      close({ returnFocus: true });
    });
    document.addEventListener("click", (event) => {
      if (isOpen() && !popover.contains(event.target) && !trigger.contains(event.target)) close();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && isOpen()) {
        event.preventDefault();
        close({ returnFocus: true });
      }
    });
    window.addEventListener("resize", () => { if (!popover.hidden) position(); }, { passive: true });
    window.addEventListener("scroll", () => { if (!popover.hidden) position(); }, { passive: true });
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", () => { if (!popover.hidden) position(); }, { passive: true });
      window.visualViewport.addEventListener("scroll", () => { if (!popover.hidden) position(); }, { passive: true });
    }
    if (typeof compactQuery.addEventListener === "function") {
      compactQuery.addEventListener("change", () => {
        syncPageState();
        if (!popover.hidden) position();
      });
    }
    monthInput.addEventListener("change", () => {
      const next = parseMonths(monthsInput.value || monthInput.value);
      if (next.join(",") !== selected.join(",")) { selected = next; sync(false); }
    });
    sync(false);
  }

  initMonthPicker();
})();
