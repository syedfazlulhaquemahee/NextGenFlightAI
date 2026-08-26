/* Flexible-date month selector. The date range picker lives in date-picker.js;
 * this control deliberately supports several candidate travel months. */
(function () {
  const firstMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
  const maxMonths = 6;

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
    popover.className = "flex-month-popover";
    popover.hidden = true;
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-label", "Choose travel months");
    popover.innerHTML = `
      <div class="flex-month-popover__head"><div><strong>When would you like to travel?</strong><span>Select one or more months to compare prices.</span></div><button type="button" class="flex-month-popover__close" aria-label="Close month picker">×</button></div>
      <div class="flex-month-grid">${choices(18).map(({ value, label }) => `<button type="button" class="flex-month-option" data-month="${value}" aria-pressed="false">${label}</button>`).join("")}</div>
      <div class="flex-month-popover__footer"><span class="flex-month-selection">No months selected</span><div><button type="button" class="flex-month-clear">Clear</button><button type="button" class="flex-month-apply">Done</button></div></div>`;
    document.body.appendChild(popover);

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

    function position() {
      const rect = trigger.getBoundingClientRect();
      const width = Math.min(448, window.innerWidth - 24);
      const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect.left));
      popover.style.width = `${width}px`;
      popover.style.left = `${left + window.scrollX}px`;
      popover.style.top = `${rect.bottom + window.scrollY + 10}px`;
    }
    function open() { position(); popover.hidden = false; trigger.setAttribute("aria-expanded", "true"); document.body.classList.add("month-roller-open"); }
    function close() { popover.hidden = true; trigger.setAttribute("aria-expanded", "false"); document.body.classList.remove("month-roller-open"); }

    monthInput._monthRollerDisplay = trigger;
    monthInput._openMonthRoller = open;
    monthInput._closeMonthRoller = close;
    trigger.addEventListener("click", (event) => { event.stopPropagation(); popover.hidden ? open() : close(); });
    trigger.addEventListener("keydown", (event) => {
      if (["Enter", " ", "ArrowDown"].includes(event.key)) { event.preventDefault(); open(); }
      if (event.key === "Escape") close();
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
    document.addEventListener("click", (event) => { if (!popover.hidden && !popover.contains(event.target) && !trigger.contains(event.target)) close(); });
    window.addEventListener("resize", () => { if (!popover.hidden) position(); }, { passive: true });
    window.addEventListener("scroll", () => { if (!popover.hidden) position(); }, { passive: true });
    monthInput.addEventListener("change", () => {
      const next = parseMonths(monthsInput.value || monthInput.value);
      if (next.join(",") !== selected.join(",")) { selected = next; sync(false); }
    });
    sync(false);
  }

  initMonthPicker();
})();
