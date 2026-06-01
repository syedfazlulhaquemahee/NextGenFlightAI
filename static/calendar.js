(function () {
  const flatpickrLib = window.flatpickr;
  if (typeof flatpickrLib !== "function") return;

  const monthSelectPlugin = typeof window.monthSelectPlugin === "function"
    ? window.monthSelectPlugin
    : null;

  const ISO_DATE_FORMAT = "Y-m-d";
  const DISPLAY_DATE_FORMAT = "M j, Y";
  const responsiveShowMonths = () => 1;
  const today = new Date();
  const firstOfCurrentMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  const responsivePickers = [];
  const viewportPadding = 12;

  const prevArrow = [
    "<svg viewBox=\"0 0 20 20\" aria-hidden=\"true\" focusable=\"false\">",
    "<path d=\"M11.75 4.75 6.5 10l5.25 5.25\" fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\" stroke-width=\"1.8\"/>",
    "</svg>",
  ].join("");

  const nextArrow = [
    "<svg viewBox=\"0 0 20 20\" aria-hidden=\"true\" focusable=\"false\">",
    "<path d=\"m8.25 4.75 5.25 5.25-5.25 5.25\" fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\" stroke-width=\"1.8\"/>",
    "</svg>",
  ].join("");

  function decorateCalendar(picker, variant) {
    if (!picker || !picker.calendarContainer) return;
    picker.calendarContainer.classList.add("standard-calendar-asset");
    picker.calendarContainer.classList.toggle("standard-calendar-asset-month", variant === "month");
  }

  function positionCalendarNearElement(picker, anchorElement) {
    if (!picker || !picker.calendarContainer) return;
    const anchor = anchorElement || picker._positionElement || picker._input || picker.input;
    if (!anchor || typeof anchor.getBoundingClientRect !== "function") return;
    picker._nxAnchorElement = anchor;

    window.requestAnimationFrame(() => {
      const calendar = picker.calendarContainer;
      if (!calendar || !calendar.classList.contains("open")) return;

      calendar.style.position = "absolute";
      calendar.style.right = "auto";
      calendar.style.bottom = "auto";
      calendar.classList.add("nx-calendar-positioned");

      const anchorRect = anchor.getBoundingClientRect();
      const rect = calendar.getBoundingClientRect();
      const width = rect.width || calendar.offsetWidth;
      const height = rect.height || calendar.offsetHeight;

      if (width <= 0 || height <= 0) return;

      const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
      const viewportHeight = window.innerHeight;
      const scrollX = window.scrollX || window.pageXOffset || 0;
      const scrollY = window.scrollY || window.pageYOffset || 0;

      const minLeft = scrollX + viewportPadding;
      const maxLeft = Math.max(minLeft, scrollX + viewportWidth - width - viewportPadding);
      const preferredLeft = scrollX + anchorRect.left + Math.min(0, (anchorRect.width - width) / 2);
      const left = Math.min(maxLeft, Math.max(minLeft, preferredLeft));

      const gap = 10;
      const belowTop = scrollY + anchorRect.bottom + gap;
      const aboveTop = scrollY + anchorRect.top - height - gap;
      const belowFits = anchorRect.bottom + gap + height <= viewportHeight - viewportPadding;
      const aboveFits = anchorRect.top - gap - height >= viewportPadding;
      const openAbove = !belowFits && aboveFits;
      const minTop = scrollY + viewportPadding;
      const maxTop = Math.max(minTop, scrollY + viewportHeight - height - viewportPadding);
      const top = Math.min(maxTop, Math.max(minTop, openAbove ? aboveTop : belowTop));

      calendar.style.left = `${Math.round(left)}px`;
      calendar.style.top = `${Math.round(top)}px`;
      calendar.classList.toggle("nx-calendar-above", openAbove);
      calendar.classList.toggle("nx-calendar-below", !openAbove);
    });
  }

  function registerResponsivePicker(picker) {
    if (!picker) return;
    responsivePickers.push(picker);
  }

  function parseIsoDate(value) {
    if (!value) return null;
    return flatpickrLib.parseDate(value, ISO_DATE_FORMAT);
  }

  function setFieldDisabled(displayInput, hiddenInput, disabled) {
    if (displayInput) {
      displayInput.disabled = disabled;
    }

    if (hiddenInput) {
      hiddenInput.disabled = disabled;
    }

    const field = displayInput
      ? displayInput.closest(".calendar-field")
      : hiddenInput?.closest(".calendar-field");

    if (field) {
      field.classList.toggle("is-disabled", disabled);
    }
  }

  function syncDatePairValues(picker, departHiddenInput, departDisplayInput, returnHiddenInput, returnDisplayInput) {
    const departDate = picker.selectedDates[0] || null;
    const returnDate = picker.selectedDates[1] || null;
    const nextDepartHiddenValue = departDate ? picker.formatDate(departDate, ISO_DATE_FORMAT) : "";
    const nextDepartDisplayValue = departDate ? picker.formatDate(departDate, DISPLAY_DATE_FORMAT) : "";
    const nextReturnHiddenValue = returnDate ? picker.formatDate(returnDate, ISO_DATE_FORMAT) : "";
    const nextReturnDisplayValue = returnDate ? picker.formatDate(returnDate, DISPLAY_DATE_FORMAT) : "";

    const notifyIfChanged = (input, nextValue) => {
      if (!input) return;
      const changed = input.value !== nextValue;
      input.value = nextValue;
      if (!changed) return;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    };

    notifyIfChanged(departHiddenInput, nextDepartHiddenValue);
    notifyIfChanged(departDisplayInput, nextDepartDisplayValue);
    notifyIfChanged(returnHiddenInput, nextReturnHiddenValue);
    notifyIfChanged(returnDisplayInput, nextReturnDisplayValue);
  }

  function buildDateOptions(mode) {
    return {
      mode,
      dateFormat: ISO_DATE_FORMAT,
      minDate: "today",
      disableMobile: true,
      allowInput: false,
      clickOpens: false,
      monthSelectorType: "static",
      showMonths: responsiveShowMonths(),
      prevArrow,
      nextArrow,
    };
  }

  function initSharedDateRange(config) {
    const {
      departHiddenInput,
      departDisplayInput,
      returnHiddenInput,
      returnDisplayInput,
      tripTypeInput,
    } = config;

    if (!departHiddenInput || !departDisplayInput || !returnHiddenInput || !returnDisplayInput) {
      return;
    }

    departDisplayInput.readOnly = true;
    returnDisplayInput.readOnly = true;

    const defaultDepartPlaceholder = departDisplayInput.getAttribute("placeholder") || "";
    const defaultReturnPlaceholder = returnDisplayInput.getAttribute("placeholder") || "";

    function syncResponsivePlaceholders() {
      const compact = window.matchMedia("(max-width: 760px)").matches;
      const oneWay = tripTypeInput?.value === "oneway";
      departDisplayInput.placeholder = compact ? "Select date" : defaultDepartPlaceholder;
      returnDisplayInput.placeholder = oneWay
        ? (compact ? "No return" : defaultReturnPlaceholder)
        : (compact ? "Add return" : defaultReturnPlaceholder);
    }

    const proxyInput = document.createElement("input");
    proxyInput.type = "text";
    proxyInput.className = "calendar-proxy-input";
    proxyInput.tabIndex = -1;
    proxyInput.setAttribute("aria-hidden", "true");

    let activeDisplayInput = departDisplayInput;

    function mountProxy(targetInput) {
      const fallbackField = departDisplayInput.closest(".calendar-field");
      const nextField = targetInput?.closest(".calendar-field") || fallbackField;
      if (!nextField) return;
      if (proxyInput.parentElement !== nextField) {
        nextField.appendChild(proxyInput);
      }
      activeDisplayInput = targetInput || departDisplayInput;
    }

    const defaultDates = [
      parseIsoDate(departHiddenInput.value),
      parseIsoDate(returnHiddenInput.value),
    ].filter(Boolean);

    mountProxy(departDisplayInput);

    const picker = flatpickrLib(proxyInput, {
      ...buildDateOptions(tripTypeInput && tripTypeInput.value === "oneway" ? "single" : "range"),
      defaultDate: defaultDates.length ? defaultDates : undefined,
      positionElement: activeDisplayInput,
      onPreCalendarPosition(_selectedDates, _dateStr, instance) {
        positionCalendarNearElement(instance, activeDisplayInput);
      },
      onReady(_selectedDates, _dateStr, instance) {
        decorateCalendar(instance);
        syncDatePairValues(instance, departHiddenInput, departDisplayInput, returnHiddenInput, returnDisplayInput);
        syncResponsivePlaceholders();
        positionCalendarNearElement(instance, activeDisplayInput);
      },
      onOpen(_selectedDates, _dateStr, instance) {
        decorateCalendar(instance);
        positionCalendarNearElement(instance, activeDisplayInput);
      },
      onChange(selectedDates, _dateStr, instance) {
        syncDatePairValues(instance, departHiddenInput, departDisplayInput, returnHiddenInput, returnDisplayInput);

        if (instance.config.mode === "single" || selectedDates.length === 2) {
          window.setTimeout(() => instance.close(), 60);
        }
      },
      onClose(_selectedDates, _dateStr, instance) {
        syncDatePairValues(instance, departHiddenInput, departDisplayInput, returnHiddenInput, returnDisplayInput);
        if (instance.config.mode === "range" && instance.selectedDates.length === 1) {
          window.setTimeout(() => instance.open(), 0);
        }
      },
    });

    window.__manualDateRangePicker = picker;
    registerResponsivePicker(picker);

    function openSharedCalendar(targetInput) {
      if (!targetInput || targetInput.disabled) return;
      mountProxy(targetInput);
      picker.set("positionElement", activeDisplayInput);
      picker._positionElement = activeDisplayInput;
      picker._nxAnchorElement = activeDisplayInput;
      picker.open();
      positionCalendarNearElement(picker, activeDisplayInput);
    }

    [departDisplayInput, returnDisplayInput].forEach((input) => {
      input.addEventListener("focus", () => openSharedCalendar(input));
      input.addEventListener("click", () => openSharedCalendar(input));
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
          event.preventDefault();
          openSharedCalendar(input);
        }
      });
    });

    if (tripTypeInput) {
      const syncTripTypeState = () => {
        const oneWay = tripTypeInput.value === "oneway";
        picker.set("mode", oneWay ? "single" : "range");

        if (oneWay && picker.selectedDates.length > 1) {
          picker.setDate([picker.selectedDates[0]], false);
        }

        if (oneWay) {
          returnHiddenInput.value = "";
          returnDisplayInput.value = "";
        } else {
          syncDatePairValues(picker, departHiddenInput, departDisplayInput, returnHiddenInput, returnDisplayInput);
        }

        setFieldDisabled(returnDisplayInput, returnHiddenInput, oneWay);
        syncResponsivePlaceholders();
      };

      tripTypeInput.addEventListener("change", syncTripTypeState);
      syncTripTypeState();
    }

    if (!tripTypeInput) {
      setFieldDisabled(returnDisplayInput, returnHiddenInput, false);
      syncResponsivePlaceholders();
    }

    window.addEventListener("resize", syncResponsivePlaceholders);
  }

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

  function initSingleDateInput(dateInput) {
    if (!dateInput || dateInput.dataset.calendarBound === "true") return null;

    dateInput.dataset.calendarBound = "true";
    dateInput.readOnly = true;

    const picker = flatpickrLib(dateInput, {
      ...buildDateOptions("single"),
      clickOpens: true,
      defaultDate: parseIsoDate(dateInput.value) || undefined,
      positionElement: dateInput,
      onPreCalendarPosition(_selectedDates, _dateStr, instance) {
        positionCalendarNearElement(instance, dateInput);
      },
      onReady(_selectedDates, _dateStr, instance) {
        decorateCalendar(instance);
        positionCalendarNearElement(instance, dateInput);
      },
      onOpen(_selectedDates, _dateStr, instance) {
        decorateCalendar(instance);
        positionCalendarNearElement(instance, dateInput);
      },
    });

    picker._nxAnchorElement = dateInput;
    registerResponsivePicker(picker);
    return picker;
  }

  function syncResponsiveCalendars() {
    const showMonths = responsiveShowMonths();
    responsivePickers.forEach((picker) => {
      if (!picker || picker.config.showMonths === showMonths) return;
      picker.set("showMonths", showMonths);
    });
  }

  function repositionOpenCalendars() {
    responsivePickers.forEach((picker) => {
      if (!picker || !picker.isOpen) return;
      positionCalendarNearElement(picker, picker._nxAnchorElement);
    });
  }

  initSharedDateRange({
    departHiddenInput: document.getElementById("departPicker"),
    departDisplayInput: document.getElementById("departPickerDisplay"),
    returnHiddenInput: document.getElementById("returnPicker"),
    returnDisplayInput: document.getElementById("returnPickerDisplay"),
    tripTypeInput: document.getElementById("tripType"),
  });

  initSharedDateRange({
    departHiddenInput: document.getElementById("refineDepartPicker"),
    departDisplayInput: document.getElementById("refineDepartPickerDisplay"),
    returnHiddenInput: document.getElementById("refineReturnPicker"),
    returnDisplayInput: document.getElementById("refineReturnPickerDisplay"),
    tripTypeInput: document.getElementById("resultsTripType"),
  });

  initMonthInput(document.getElementById("flexMonthPicker"));
  window.initSingleCalendarInput = initSingleDateInput;
  document.querySelectorAll('input[name="leg_date"]').forEach(initSingleDateInput);

  window.addEventListener("resize", () => {
    syncResponsiveCalendars();
    repositionOpenCalendars();
  }, { passive: true });
  window.addEventListener("scroll", repositionOpenCalendars, { passive: true });
})();
