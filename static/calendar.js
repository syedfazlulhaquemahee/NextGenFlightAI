/*
 * Flex "cheapest week" month picker (a custom popover, not a calendar
 * library). Depart/return date pickers now live in date-picker.js, built on
 * the Cally web components — this file only handles the month roller.
 */
(function () {
  const today = new Date();
  const firstOfCurrentMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  const viewportPadding = 12;

  function formatMonthDisplay(value) {
    if (!/^\d{4}-\d{2}$/.test(value || "")) return "";
    const [year, month] = value.split("-").map(Number);
    const date = new Date(year, month - 1, 1);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric" }).format(date);
  }

  function formatMonthValue(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    return `${year}-${month}`;
  }

  function buildMonthChoices(count = 24) {
    const choices = [];
    for (let index = 0; index < count; index += 1) {
      const date = new Date(firstOfCurrentMonth.getFullYear(), firstOfCurrentMonth.getMonth() + index, 1);
      choices.push({
        value: formatMonthValue(date),
        label: new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric" }).format(date),
      });
    }
    return choices;
  }

  function initMonthInput(monthInput) {
    if (!monthInput || monthInput.dataset.monthRollerBound === "true") return;

    monthInput.dataset.monthRollerBound = "true";
    monthInput.type = "hidden";

    const field = monthInput.closest(".calendar-field") || monthInput.parentElement;
    const displayInput = document.createElement("input");
    displayInput.type = "text";
    displayInput.className = `${monthInput.className || ""} month-roller-display`.trim();
    displayInput.id = `${monthInput.id}Display`;
    displayInput.placeholder = monthInput.getAttribute("placeholder") || "Select month";
    displayInput.setAttribute("aria-label", monthInput.getAttribute("aria-label") || "Month");
    displayInput.setAttribute("aria-haspopup", "listbox");
    displayInput.setAttribute("aria-expanded", "false");
    displayInput.readOnly = true;
    displayInput.value = formatMonthDisplay(monthInput.value);
    monthInput._monthRollerDisplay = displayInput;
    monthInput.insertAdjacentElement("afterend", displayInput);

    const popover = document.createElement("div");
    popover.className = "month-roller-popover";
    popover.setAttribute("role", "listbox");
    popover.setAttribute("aria-label", "Choose month");
    popover.hidden = true;

    const choices = buildMonthChoices();
    popover.innerHTML = [
      "<div class=\"month-roller-head\">Choose month</div>",
      "<div class=\"month-roller-list\">",
      choices.map((choice) => (
        `<button type="button" class="month-roller-option" role="option" data-month-value="${choice.value}">${choice.label}</button>`
      )).join(""),
      "</div>",
    ].join("");
    document.body.appendChild(popover);

    const list = popover.querySelector(".month-roller-list");
    const options = Array.from(popover.querySelectorAll(".month-roller-option"));
    let suppressNextFocusOpen = false;

    function setMonth(value, notify = true) {
      monthInput.value = value;
      displayInput.value = formatMonthDisplay(value);
      monthInput.setCustomValidity("");
      displayInput.setCustomValidity("");
      options.forEach((option) => {
        const active = option.dataset.monthValue === value;
        option.classList.toggle("is-selected", active);
        option.setAttribute("aria-selected", String(active));
      });
      if (notify) {
        monthInput.dispatchEvent(new Event("input", { bubbles: true }));
        monthInput.dispatchEvent(new Event("change", { bubbles: true }));
        displayInput.dispatchEvent(new Event("input", { bubbles: true }));
        displayInput.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    function positionPopover() {
      const rect = displayInput.getBoundingClientRect();
      const width = Math.max(220, Math.min(rect.width, window.innerWidth - viewportPadding * 2));
      const maxLeft = window.scrollX + window.innerWidth - width - viewportPadding;
      const left = Math.max(window.scrollX + viewportPadding, Math.min(maxLeft, window.scrollX + rect.left));
      popover.style.width = `${width}px`;
      popover.style.left = `${left}px`;
      popover.style.top = `${window.scrollY + rect.bottom + 8}px`;
    }

    function openPopover() {
      positionPopover();
      popover.hidden = false;
      document.body.classList.add("month-roller-open");
      displayInput.setAttribute("aria-expanded", "true");
      const active = popover.querySelector(".month-roller-option.is-selected") || options[0];
      window.requestAnimationFrame(() => {
        if (!active || !list) return;
        const top = active.offsetTop - ((list.clientHeight - active.offsetHeight) / 2);
        list.scrollTop = Math.max(0, top);
      });
    }

    function closePopover() {
      popover.hidden = true;
      document.body.classList.remove("month-roller-open");
      displayInput.setAttribute("aria-expanded", "false");
    }

    function isOpen() {
      return !popover.hidden;
    }

    monthInput._openMonthRoller = openPopover;
    monthInput._closeMonthRoller = closePopover;

    displayInput.addEventListener("click", (event) => {
      event.stopPropagation();
      openPopover();
    });

    displayInput.addEventListener("focus", () => {
      if (suppressNextFocusOpen) {
        suppressNextFocusOpen = false;
        return;
      }
      openPopover();
    });

    displayInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closePopover();
        return;
      }
      if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
        event.preventDefault();
        openPopover();
      }
    });

    popover.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
    });

    popover.addEventListener("click", (event) => {
      event.stopPropagation();
      const option = event.target.closest(".month-roller-option");
      if (!option) return;
      setMonth(option.dataset.monthValue || "");
      closePopover();
      suppressNextFocusOpen = true;
      try {
        displayInput.focus({ preventScroll: true });
      } catch (e) {
        displayInput.focus();
      }
      window.setTimeout(() => {
        suppressNextFocusOpen = false;
      }, 0);
    });

    document.addEventListener("click", (event) => {
      if (popover.hidden) return;
      if (popover.contains(event.target) || field?.contains(event.target)) return;
      closePopover();
    });

    window.addEventListener("resize", () => {
      if (isOpen()) positionPopover();
    }, { passive: true });

    window.addEventListener("scroll", () => {
      if (isOpen()) positionPopover();
    }, { passive: true });

    setMonth(monthInput.value, false);
  }

  initMonthInput(document.getElementById("flexMonthPicker"));
})();
