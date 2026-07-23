/*
 * Self-contained date picker built on the Cally calendar web components
 * (<calendar-date>, <calendar-range>, <calendar-month> — vendored at
 * vendor/cally/cally.js). Replaces the previous flatpickr-based widget.
 *
 * Design notes (why this shape, not flatpickr's):
 * - The popover shell here owns 100% of its own open/close/position logic.
 *   It only closes on an explicit outside click, Escape, the Done button, or
 *   a completed selection — never as a side effect of scroll/resize. Those
 *   two events only ever reposition it, matching the pattern already proven
 *   safe elsewhere in this codebase (the travelers panel, the month picker).
 * - Range selection (depart + return) is handled natively by <calendar-range>
 *   with its own internal "first click pending" state, so there is no need
 *   for the old reopen-if-only-one-date-picked hack that caused visible
 *   flicker/closing.
 * - Popover is positioned with `position: absolute` in document coordinates
 *   (scrollX/scrollY + anchor rect), so it scrolls naturally with the page
 *   without needing to fight the browser on every scroll frame.
 */
(function () {
  "use strict";

  if (window.__nxDatePickerLoaded) return;
  window.__nxDatePickerLoaded = true;

  var ISO_RE = /^\d{4}-\d{2}-\d{2}$/;
  var VIEWPORT_PADDING = 12;
  var compactQuery = window.matchMedia("(max-width: 760px)");
  var openRebuilders = [];

  // Cally's previous/next month buttons default to literal "Previous"/
  // "Next" text via named slots — replace that with compact chevron icons
  // (same stroke style as the rest of this app's icon set) so the buttons
  // stay small instead of overflowing with a full word.
  var PREV_ICON_SVG =
    '<svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">' +
    '<path d="M11.75 4.75 6.5 10l5.25 5.25" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8"/>' +
    "</svg>";
  var NEXT_ICON_SVG =
    '<svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">' +
    '<path d="m8.25 4.75 5.25 5.25-5.25 5.25" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8"/>' +
    "</svg>";

  function addNavIcons(calendarEl) {
    var prev = document.createElement("span");
    prev.setAttribute("slot", "previous");
    prev.innerHTML = PREV_ICON_SVG;
    var next = document.createElement("span");
    next.setAttribute("slot", "next");
    next.innerHTML = NEXT_ICON_SVG;
    calendarEl.appendChild(prev);
    calendarEl.appendChild(next);
  }

  function calendarsDefined() {
    return !!(window.customElements && customElements.get("calendar-range"));
  }

  function whenCalendarsReady(cb) {
    if (calendarsDefined()) {
      cb();
      return;
    }
    if (window.customElements && customElements.whenDefined) {
      customElements.whenDefined("calendar-range").then(cb);
    } else {
      // Extremely old browser without customElements — nothing we can do.
    }
  }

  function todayIso() {
    var d = new Date();
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function formatDisplay(iso) {
    if (!ISO_RE.test(iso || "")) return "";
    var parts = iso.split("-").map(Number);
    var date = new Date(parts[0], parts[1] - 1, parts[2]);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(date);
  }

  function notifyIfChanged(input, nextValue) {
    if (!input) return;
    if (input.value === nextValue) return;
    input.value = nextValue;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // ------------------------------------------------------------------
  // Popover shell: shared by both the range picker and single-date picker.
  // ------------------------------------------------------------------
  function createPopover(kind) {
    var root = document.createElement("div");
    root.className = "ndp-popover ndp-" + kind;
    root.setAttribute("role", "dialog");
    root.hidden = true;

    var hint = document.createElement("div");
    hint.className = "ndp-hint";
    root.appendChild(hint);

    var body = document.createElement("div");
    body.className = "ndp-body";
    root.appendChild(body);

    var footer = document.createElement("div");
    footer.className = "ndp-footer";
    var clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "ndp-btn ndp-btn--ghost";
    clearBtn.textContent = "Clear";
    var doneBtn = document.createElement("button");
    doneBtn.type = "button";
    doneBtn.className = "ndp-btn ndp-btn--primary";
    doneBtn.textContent = "Done";
    footer.appendChild(clearBtn);
    footer.appendChild(doneBtn);
    root.appendChild(footer);

    document.body.appendChild(root);

    var anchor = null;
    var onOutside = null;
    var onKey = null;

    function position() {
      if (root.hidden || !anchor) return;
      var anchorRect = anchor.getBoundingClientRect();
      var rect = root.getBoundingClientRect();
      var width = rect.width || root.offsetWidth;
      var height = rect.height || root.offsetHeight;
      if (!width || !height) return;

      var viewportWidth = document.documentElement.clientWidth || window.innerWidth;
      var viewportHeight = window.innerHeight;
      var scrollX = window.scrollX || window.pageXOffset || 0;
      var scrollY = window.scrollY || window.pageYOffset || 0;

      var minLeft = scrollX + VIEWPORT_PADDING;
      var maxLeft = Math.max(minLeft, scrollX + viewportWidth - width - VIEWPORT_PADDING);
      var preferredLeft = scrollX + anchorRect.left;
      var left = Math.min(maxLeft, Math.max(minLeft, preferredLeft));

      var gap = 10;
      var belowTop = scrollY + anchorRect.bottom + gap;
      var aboveTop = scrollY + anchorRect.top - height - gap;
      var belowFits = anchorRect.bottom + gap + height <= viewportHeight - VIEWPORT_PADDING;
      var aboveFits = anchorRect.top - gap - height >= VIEWPORT_PADDING;
      var openAbove = !belowFits && aboveFits;
      var minTop = scrollY + VIEWPORT_PADDING;
      var maxTop = Math.max(minTop, scrollY + viewportHeight - height - VIEWPORT_PADDING);
      var top = Math.min(maxTop, Math.max(minTop, openAbove ? aboveTop : belowTop));

      root.style.left = Math.round(left) + "px";
      root.style.top = Math.round(top) + "px";
      root.classList.toggle("ndp-above", openAbove);
      root.classList.toggle("ndp-below", !openAbove);
    }

    function reposition() {
      window.requestAnimationFrame(position);
    }

    function close() {
      if (root.hidden) return;
      root.hidden = true;
      document.body.classList.remove("ndp-open");
      if (onOutside) document.removeEventListener("pointerdown", onOutside, true);
      if (onKey) document.removeEventListener("keydown", onKey, true);
      onOutside = null;
      onKey = null;
    }

    function open(nextAnchor) {
      anchor = nextAnchor || anchor;
      if (!root.hidden) {
        reposition();
        return;
      }
      root.hidden = false;
      document.body.classList.add("ndp-open");
      reposition();

      onOutside = function (e) {
        if (root.contains(e.target)) return;
        if (anchor && typeof anchor.contains === "function" && anchor.contains(e.target)) return;
        close();
      };
      onKey = function (e) {
        if (e.key === "Escape") {
          close();
          if (anchor && typeof anchor.focus === "function") {
            try { anchor.focus({ preventScroll: true }); } catch (err) { anchor.focus(); }
          }
        }
      };
      // Defer binding so the click that opened the popover doesn't
      // immediately register as an "outside" click and close it again.
      window.setTimeout(function () {
        document.addEventListener("pointerdown", onOutside, true);
        document.addEventListener("keydown", onKey, true);
      }, 0);
    }

    window.addEventListener("resize", reposition, { passive: true });
    window.addEventListener("scroll", reposition, { passive: true });

    return {
      root: root,
      hint: hint,
      body: body,
      clearBtn: clearBtn,
      doneBtn: doneBtn,
      open: open,
      close: close,
      reposition: reposition,
      isOpen: function () { return !root.hidden; },
    };
  }

  function monthsToShow(mode) {
    if (compactQuery.matches) return 1;
    return mode === "range" ? 2 : 1;
  }

  // ------------------------------------------------------------------
  // Shared depart/return range picker (round-trip <-> one-way aware).
  // ------------------------------------------------------------------
  function initSharedDateRange(config) {
    var departHiddenInput = config.departHiddenInput;
    var departDisplayInput = config.departDisplayInput;
    var returnHiddenInput = config.returnHiddenInput;
    var returnDisplayInput = config.returnDisplayInput;
    var tripTypeInput = config.tripTypeInput;

    if (!departHiddenInput || !departDisplayInput || !returnHiddenInput || !returnDisplayInput) return null;

    departDisplayInput.readOnly = true;
    returnDisplayInput.readOnly = true;

    var defaultDepartPlaceholder = departDisplayInput.getAttribute("placeholder") || "";
    var defaultReturnPlaceholder = returnDisplayInput.getAttribute("placeholder") || "";

    function syncResponsivePlaceholders() {
      var compact = compactQuery.matches;
      var oneWay = tripTypeInput && tripTypeInput.value === "oneway";
      departDisplayInput.placeholder = compact ? "Select date" : defaultDepartPlaceholder;
      returnDisplayInput.placeholder = oneWay
        ? (compact ? "No return" : defaultReturnPlaceholder)
        : (compact ? "Add return" : defaultReturnPlaceholder);
    }

    function setReturnDisabled(disabled) {
      returnDisplayInput.disabled = disabled;
      returnHiddenInput.disabled = disabled;
      var field = returnDisplayInput.closest(".calendar-field");
      if (field) field.classList.toggle("is-disabled", disabled);
    }

    var popover = createPopover("range");
    var calendarEl = null;
    var mode = tripTypeInput && tripTypeInput.value === "oneway" ? "single" : "range";

    function syncFromCalendar() {
      if (!calendarEl) return;
      if (mode === "range") {
        var raw = calendarEl.value || "";
        var pieces = raw.split("/");
        var dep = pieces[0] || "";
        var ret = pieces[1] || "";
        notifyIfChanged(departHiddenInput, ISO_RE.test(dep) ? dep : "");
        notifyIfChanged(departDisplayInput, formatDisplay(dep));
        notifyIfChanged(returnHiddenInput, ISO_RE.test(ret) ? ret : "");
        notifyIfChanged(returnDisplayInput, formatDisplay(ret));
      } else {
        var single = calendarEl.value || "";
        notifyIfChanged(departHiddenInput, ISO_RE.test(single) ? single : "");
        notifyIfChanged(departDisplayInput, formatDisplay(single));
      }
    }

    function buildCalendar() {
      popover.body.innerHTML = "";
      var el = document.createElement(mode === "range" ? "calendar-range" : "calendar-date");
      el.setAttribute("min", todayIso());

      var count = monthsToShow(mode);
      for (var i = 0; i < count; i += 1) {
        var monthEl = document.createElement("calendar-month");
        if (i > 0) monthEl.setAttribute("offset", String(i));
        el.appendChild(monthEl);
      }
      popover.root.classList.toggle("ndp-two-col", count > 1);

      var dep = departHiddenInput.value || "";
      var ret = returnHiddenInput.value || "";
      if (mode === "range" && ISO_RE.test(dep) && ISO_RE.test(ret)) {
        el.setAttribute("value", dep + "/" + ret);
      } else if (mode === "single" && ISO_RE.test(dep)) {
        el.setAttribute("value", dep);
      }

      el.addEventListener("rangestart", function () {
        popover.hint.textContent = "Choose your return date";
        popover.hint.classList.add("is-active");
      });

      el.addEventListener("change", function () {
        syncFromCalendar();
        popover.hint.classList.remove("is-active");
        popover.hint.textContent = mode === "range" ? "Choose your departure date" : "Departure date set";
        window.setTimeout(function () { popover.close(); }, mode === "range" ? 260 : 160);
      });

      addNavIcons(el);
      popover.body.appendChild(el);
      calendarEl = el;
      popover.hint.classList.remove("is-active");
      popover.hint.textContent = "Choose your departure date";
      popover.reposition();
    }

    popover.clearBtn.addEventListener("click", function () {
      notifyIfChanged(departHiddenInput, "");
      notifyIfChanged(departDisplayInput, "");
      if (mode === "range") {
        notifyIfChanged(returnHiddenInput, "");
        notifyIfChanged(returnDisplayInput, "");
      }
      buildCalendar();
    });
    popover.doneBtn.addEventListener("click", function () { popover.close(); });

    function openPopoverFor(field) {
      if (!field || field.disabled) return;
      whenCalendarsReady(function () {
        buildCalendar();
        popover.open(field);
      });
    }

    [departDisplayInput, returnDisplayInput].forEach(function (input) {
      input.addEventListener("focus", function () { openPopoverFor(input); });
      input.addEventListener("click", function () { openPopoverFor(input); });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown") {
          e.preventDefault();
          openPopoverFor(input);
        }
      });
    });

    if (tripTypeInput) {
      var syncTripType = function () {
        var oneWay = tripTypeInput.value === "oneway";
        var nextMode = oneWay ? "single" : "range";
        if (nextMode !== mode) {
          mode = nextMode;
          if (oneWay) {
            notifyIfChanged(returnHiddenInput, "");
            notifyIfChanged(returnDisplayInput, "");
          }
          if (popover.isOpen()) buildCalendar();
        }
        setReturnDisabled(oneWay);
        syncResponsivePlaceholders();
      };
      tripTypeInput.addEventListener("change", syncTripType);
      syncTripType();
    } else {
      setReturnDisabled(false);
      syncResponsivePlaceholders();
    }

    openRebuilders.push(function () {
      if (popover.isOpen()) buildCalendar();
    });

    window.addEventListener("resize", syncResponsivePlaceholders);

    return { close: function () { popover.close(); } };
  }

  // ------------------------------------------------------------------
  // Standalone single-date input (multi-city legs, etc).
  // ------------------------------------------------------------------
  function initSingleDateInput(input) {
    if (!input || input.dataset.calendarBound === "true") return null;
    input.dataset.calendarBound = "true";
    input.readOnly = true;

    var popover = createPopover("single");
    var minIso = todayIso();

    function buildCalendar() {
      popover.body.innerHTML = "";
      var el = document.createElement("calendar-date");
      el.setAttribute("min", minIso);
      var monthEl = document.createElement("calendar-month");
      el.appendChild(monthEl);
      if (ISO_RE.test(input.value || "")) el.setAttribute("value", input.value);

      el.addEventListener("change", function () {
        var next = el.value || "";
        notifyIfChanged(input, ISO_RE.test(next) ? next : "");
        window.setTimeout(function () { popover.close(); }, 160);
      });

      addNavIcons(el);
      popover.body.appendChild(el);
      popover.hint.textContent = "Choose a date";
      popover.reposition();
    }

    popover.clearBtn.addEventListener("click", function () {
      notifyIfChanged(input, "");
      buildCalendar();
    });
    popover.doneBtn.addEventListener("click", function () { popover.close(); });

    function openPopover() {
      if (input.disabled) return;
      whenCalendarsReady(function () {
        buildCalendar();
        popover.open(input);
      });
    }

    input.addEventListener("focus", openPopover);
    input.addEventListener("click", openPopover);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown") {
        e.preventDefault();
        openPopover();
      }
    });

    openRebuilders.push(function () {
      if (popover.isOpen()) buildCalendar();
    });

    var wrapper = {
      setMinDate: function (iso) {
        minIso = ISO_RE.test(iso || "") ? iso : todayIso();
        if (popover.isOpen()) buildCalendar();
      },
      destroy: function () {
        popover.close();
        if (popover.root.parentElement) popover.root.parentElement.removeChild(popover.root);
        delete input.dataset.calendarBound;
        input._nxDatePicker = null;
      },
      close: function () { popover.close(); },
    };
    input._nxDatePicker = wrapper;
    return wrapper;
  }

  // Rebuild any *open* calendars when crossing the compact/desktop breakpoint
  // so the month count (1 vs 2) stays correct without needing to close first.
  if (typeof compactQuery.addEventListener === "function") {
    compactQuery.addEventListener("change", function () {
      openRebuilders.forEach(function (fn) { fn(); });
    });
  }

  window.initSingleCalendarInput = initSingleDateInput;
  window.initSharedDateRange = initSharedDateRange;

  var mainRangePicker = initSharedDateRange({
    departHiddenInput: document.getElementById("departPicker"),
    departDisplayInput: document.getElementById("departPickerDisplay"),
    returnHiddenInput: document.getElementById("returnPicker"),
    returnDisplayInput: document.getElementById("returnPickerDisplay"),
    tripTypeInput: document.getElementById("tripType"),
  });
  window.__manualDateRangePicker = mainRangePicker;

  initSharedDateRange({
    departHiddenInput: document.getElementById("refineDepartPicker"),
    departDisplayInput: document.getElementById("refineDepartPickerDisplay"),
    returnHiddenInput: document.getElementById("refineReturnPicker"),
    returnDisplayInput: document.getElementById("refineReturnPickerDisplay"),
    tripTypeInput: document.getElementById("resultsTripType"),
  });

  document.querySelectorAll('input[name="leg_date"]').forEach(function (input) {
    initSingleDateInput(input);
  });
})();
