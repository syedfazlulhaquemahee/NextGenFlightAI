(function () {
  const root = document.querySelector(".results-page");
  if (!root) return;

  const cardsEl = document.getElementById("resultsCards");
  if (!cardsEl) return;

  const editToggle = document.getElementById("rfEditToggle");
  const editBar = document.getElementById("rfEditBar");
  const editForm = document.getElementById("rfEditForm");
  const tripTypeInput = document.getElementById("rfTripTypeInput");
  const originInput = document.getElementById("rfOriginInput");
  const destinationInput = document.getElementById("rfDestinationInput");
  const departInput = document.getElementById("refineDepartPicker");
  const returnInput = document.getElementById("refineReturnPicker");
  const departDisplayInput = document.getElementById("refineDepartPickerDisplay");
  const returnDisplayInput = document.getElementById("refineReturnPickerDisplay");
  const dateRangeButton = document.getElementById("rfDateRangeButton");
  const dateRangeValue = document.getElementById("rfDateRangeValue");
  const swapBtn = document.getElementById("rfSwapAirports");

  const filterToggle = document.getElementById("rfFilterToggle");
  const filterBackdrop = document.getElementById("rfFilterBackdrop");
  const sidebar = document.getElementById("rfSidebar");
  const sidebarClose = document.getElementById("rfSidebarClose");

  const visibleCountEl = document.getElementById("rfVisibleCount");
  const visibleTextEl = document.getElementById("rfVisibleText");
  const resultsCopyEl = document.getElementById("rfResultsCopy");
  const priceLabelEl = document.getElementById("rfPriceLabel");
  const durationLabelEl = document.getElementById("rfDurationLabel");
  const airlinesEl = document.getElementById("rfAirlines");
  const airportsEl = document.getElementById("rfAirports");
  const appliedEl = document.getElementById("rfApplied");
  const applyBtn = document.getElementById("rfApplyFilters");
  const clearBtn = document.getElementById("rfClearFilters");
  const clearLinkBtn = document.getElementById("rfSidebarClearLink");
  const priceMinEl = document.getElementById("rfPriceMin");
  const priceMaxEl = document.getElementById("rfPriceMax");
  const durationMaxEl = document.getElementById("rfDurationMax");
  const toolbarSortCopyEl = document.querySelector(".rf-toolbar-sortcopy");
  const sortMenu = document.getElementById("rfSortMenu");
  const sortTrigger = document.getElementById("rfSortTrigger");
  const sortDropdown = document.getElementById("rfSortDropdown");

  const sortButtons = Array.from(document.querySelectorAll("[data-sort]"));
  const stopButtons = Array.from(document.querySelectorAll("[data-stop]"));
  const departWindowButtons = Array.from(document.querySelectorAll("[data-depart-window]"));
  const arrivalWindowButtons = Array.from(document.querySelectorAll("[data-arrival-window]"));
  const bagButtons = Array.from(document.querySelectorAll("[data-bag]"));
  const refundButtons = Array.from(document.querySelectorAll("[data-refundability]"));
  const tripButtons = Array.from(document.querySelectorAll("[data-trip-type]"));

  const sortLabels = {
    recommended: "by AI recommendation",
    best: "by best overall value",
    cheapest: "by lowest price",
    fastest: "by shortest duration",
    "earliest-departure": "by earliest departure",
    "earliest-arrival": "by earliest arrival",
  };

  let cards = [];
  let metricsCache = null;

  const state = {
    sort: document.querySelector(".rf-sort-option.is-active")?.dataset.sort || "recommended",
    stops: new Set(),
    departWindows: new Set(),
    arrivalWindows: new Set(),
    bags: new Set(),
    airlines: new Set(),
    airports: new Set(),
    refundability: new Set(),
    priceMin: 0,
    priceMax: 0,
    durationMax: 0,
  };

  const savedFlights = new Set();

  function loadSavedFlights() {
    try {
      const raw = window.localStorage.getItem("rf-saved-flights");
      const parsed = raw ? JSON.parse(raw) : [];
      if (Array.isArray(parsed)) {
        parsed.forEach((id) => {
          if (typeof id === "string" && id) savedFlights.add(id);
        });
      }
    } catch (error) {}
  }

  function persistSavedFlights() {
    try {
      window.localStorage.setItem("rf-saved-flights", JSON.stringify(Array.from(savedFlights)));
    } catch (error) {}
  }

  function toNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function formatMoney(value) {
    return `$${Math.round(toNumber(value, 0))}`;
  }

  function formatDuration(minutes) {
    const total = Math.max(0, Math.round(toNumber(minutes, 0)));
    const hours = Math.floor(total / 60);
    const mins = total % 60;
    if (!hours) return `${mins}m max`;
    if (!mins) return `${hours}h max`;
    return `${hours}h ${mins}m max`;
  }

  function formatShortDate(iso) {
    if (!iso) return "";
    const parsed = new Date(`${iso}T00:00:00`);
    if (Number.isNaN(parsed.getTime())) return iso;
    return new Intl.DateTimeFormat("en-US", {
      month: "short",
      day: "numeric",
    }).format(parsed);
  }

  function parseMinute(iso) {
    if (!iso) return null;
    const parsed = new Date(String(iso));
    if (Number.isNaN(parsed.getTime())) return null;
    return parsed.getHours() * 60 + parsed.getMinutes();
  }

  function timeBucket(iso) {
    const minute = parseMinute(iso);
    if (minute == null) return "";
    if (minute < 360) return "red-eye";
    if (minute < 720) return "morning";
    if (minute < 1080) return "afternoon";
    return "evening";
  }

  function collectCards() {
    cards = Array.from(cardsEl.querySelectorAll(".rf-card")).filter((card) => !card.classList.contains("flex-live-skeleton"));
    cards.forEach((card, index) => {
      if (!card.dataset.originalIndex) {
        card.dataset.originalIndex = String(index);
      }
    });
    metricsCache = null;
    syncSavedButtons();
  }

  function getMetrics() {
    if (metricsCache) return metricsCache;

    const metrics = {
      minPrice: Infinity,
      maxPrice: 0,
      maxDuration: 0,
      airlines: new Map(),
      airports: new Map(),
    };

    cards.forEach((card) => {
      const price = toNumber(card.dataset.price, 0);
      const duration = toNumber(card.dataset.durationMin, 0);
      const airline = String(card.dataset.airline || "Unknown airline").trim();
      metrics.minPrice = Math.min(metrics.minPrice, price);
      metrics.maxPrice = Math.max(metrics.maxPrice, price);
      metrics.maxDuration = Math.max(metrics.maxDuration, duration);
      metrics.airlines.set(airline, (metrics.airlines.get(airline) || 0) + 1);

      String(card.dataset.airports || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean)
        .forEach((code) => {
          metrics.airports.set(code, (metrics.airports.get(code) || 0) + 1);
        });
    });

    if (!Number.isFinite(metrics.minPrice)) metrics.minPrice = 0;
    metricsCache = metrics;
    return metrics;
  }

  function bestScore(card) {
    const metrics = getMetrics();
    const minPrice = metrics.minPrice;
    const maxPrice = Math.max(minPrice + 1, metrics.maxPrice);
    const maxDuration = Math.max(1, metrics.maxDuration);
    const price = toNumber(card.dataset.price, 0);
    const duration = toNumber(card.dataset.durationMin, 0);
    const stops = toNumber(card.dataset.totalStops, 0);
    const trust = toNumber(card.dataset.trustScore, 80);
    const priceFactor = 1 - (price - minPrice) / (maxPrice - minPrice || 1);
    const durationFactor = 1 - duration / maxDuration;
    return trust * 0.65 + priceFactor * 22 + durationFactor * 14 - stops * 6;
  }

  function updateSortInputs() {
    document.querySelectorAll('input[name="sort"]').forEach((input) => {
      input.value = state.sort === "best" ? "recommended" : state.sort;
    });
    if (toolbarSortCopyEl) {
      const label = state.sort === "recommended"
        ? "Recommended by AI"
        : state.sort === "best"
          ? "Best"
          : state.sort === "cheapest"
            ? "Cheapest"
            : state.sort === "fastest"
              ? "Fastest"
              : state.sort === "earliest-arrival"
                ? "Earliest arrival"
                : "Earliest departure";
      toolbarSortCopyEl.textContent = `Sort by: ${label}`;
    }
    sortButtons.forEach((chip) => chip.classList.toggle("is-active", chip.dataset.sort === state.sort));
  }

  function updateTripTypeUi() {
    const isOneWay = tripTypeInput?.value === "oneway";
    root.classList.toggle("rf-oneway", isOneWay);
    tripButtons.forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.tripType === tripTypeInput.value);
    });
    if (returnInput) {
      returnInput.disabled = isOneWay;
      if (isOneWay) returnInput.value = "";
    }
  }

  function updateDateRangeSummary() {
    if (!dateRangeValue) return;
    const depart = String(departInput?.value || "").trim();
    const ret = String(returnInput?.value || "").trim();
    if (depart && ret) {
      dateRangeValue.textContent = `${formatShortDate(depart)} – ${formatShortDate(ret)}`;
      return;
    }
    if (depart) {
      dateRangeValue.textContent = formatShortDate(depart);
      return;
    }
    dateRangeValue.textContent = "Select dates";
  }

  function setSidebarOpen(open) {
    root.classList.toggle("rf-sidebar-open", open);
  }

  function renderChecklist(target, entries, selectedSet, dataKey) {
    if (!target) return;
    target.innerHTML = entries
      .map(([label, count]) => {
        const checked = selectedSet.size === 0 || selectedSet.has(label);
        const safe = label.replace(/"/g, "&quot;");
        return `<label class="rf-airline-item"><input type="checkbox" data-${dataKey}="${safe}" ${checked ? "checked" : ""} /><span>${label}</span><strong>${count}</strong></label>`;
      })
      .join("");
  }

  function renderDynamicFilters() {
    const metrics = getMetrics();
    const airlines = Array.from(metrics.airlines.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    const airports = Array.from(metrics.airports.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    renderChecklist(airlinesEl, airlines, state.airlines, "airline");
    renderChecklist(airportsEl, airports, state.airports, "airport");
  }

  function updateRangeControls() {
    const metrics = getMetrics();
    const min = Math.floor(metrics.minPrice);
    const max = Math.max(min + 1, Math.ceil(metrics.maxPrice));

    if (priceMinEl && priceMaxEl) {
      priceMinEl.min = String(min);
      priceMinEl.max = String(max);
      priceMaxEl.min = String(min);
      priceMaxEl.max = String(max);

      if (!state.priceMin && !state.priceMax) {
        state.priceMin = min;
        state.priceMax = max;
      } else {
        state.priceMin = Math.max(min, Math.min(state.priceMin, max));
        state.priceMax = Math.max(state.priceMin, Math.min(state.priceMax, max));
      }

      priceMinEl.value = String(state.priceMin);
      priceMaxEl.value = String(state.priceMax);
      if (priceLabelEl) priceLabelEl.textContent = `${formatMoney(state.priceMin)} – ${formatMoney(state.priceMax)}`;
    }

    if (durationMaxEl) {
      const maxDuration = Math.max(60, Math.ceil(metrics.maxDuration || 60));
      durationMaxEl.max = String(maxDuration);
      if (!state.durationMax) {
        state.durationMax = maxDuration;
      } else {
        state.durationMax = Math.min(state.durationMax, maxDuration);
      }
      durationMaxEl.value = String(state.durationMax);
      if (durationLabelEl) durationLabelEl.textContent = state.durationMax >= maxDuration ? "Any duration" : formatDuration(state.durationMax);
    }
  }

  function sortCards() {
    const ordered = cards.slice().sort((a, b) => {
      switch (state.sort) {
        case "best":
          return bestScore(b) - bestScore(a);
        case "cheapest":
          return toNumber(a.dataset.price) - toNumber(b.dataset.price);
        case "fastest":
          return toNumber(a.dataset.durationMin) - toNumber(b.dataset.durationMin);
        case "earliest-departure":
          return (parseMinute(a.dataset.outDepartIso) ?? 9999) - (parseMinute(b.dataset.outDepartIso) ?? 9999);
        case "earliest-arrival":
          return (parseMinute(a.dataset.outArriveIso) ?? 9999) - (parseMinute(b.dataset.outArriveIso) ?? 9999);
        default:
          return toNumber(a.dataset.originalIndex) - toNumber(b.dataset.originalIndex);
      }
    });

    ordered.forEach((card) => cardsEl.appendChild(card));
  }

  function cardMatches(card) {
    const price = toNumber(card.dataset.price, 0);
    const duration = toNumber(card.dataset.durationMin, 0);
    const stops = String(card.dataset.totalStops || "0");
    const airline = String(card.dataset.airline || "").trim();
    const departWindow = timeBucket(card.dataset.outDepartIso);
    const arrivalWindow = timeBucket(card.dataset.outArriveIso);
    const airports = String(card.dataset.airports || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const hasCarry = card.dataset.hasCarryOn === "1";
    const hasChecked = card.dataset.hasChecked === "1";
    const refundable = card.dataset.refundable === "1";

    if (price < state.priceMin || price > state.priceMax) return false;
    if (duration > state.durationMax) return false;
    if (state.stops.size && !state.stops.has(stops)) return false;
    if (state.departWindows.size && !state.departWindows.has(departWindow)) return false;
    if (state.arrivalWindows.size && !state.arrivalWindows.has(arrivalWindow)) return false;
    if (state.airlines.size && !state.airlines.has(airline)) return false;
    if (state.airports.size && !airports.some((airport) => state.airports.has(airport))) return false;
    if (state.refundability.size && !state.refundability.has(refundable ? "1" : "0")) return false;
    if (state.bags.has("carry-on") && !hasCarry) return false;
    if (state.bags.has("checked") && !hasChecked) return false;
    return true;
  }

  function updateApplied() {
    if (!appliedEl) return;
    const chips = [];

    state.stops.forEach((stop) => chips.push(stop === "0" ? "Nonstop" : stop === "1" ? "1 stop" : "2+ stops"));
    state.departWindows.forEach((value) => chips.push(`Depart ${value.replace("-", " ")}`));
    state.arrivalWindows.forEach((value) => chips.push(`Arrive ${value.replace("-", " ")}`));
    state.bags.forEach((value) => chips.push(value === "carry-on" ? "Carry-on included" : "Checked bag included"));
    state.airlines.forEach((value) => chips.push(value));
    state.airports.forEach((value) => chips.push(value));
    state.refundability.forEach((value) => chips.push(value === "1" ? "Refundable or flexible" : "Standard / restricted"));

    if (priceMinEl && priceMaxEl) {
      if (state.priceMin !== toNumber(priceMinEl.min) || state.priceMax !== toNumber(priceMaxEl.max)) {
        chips.push(`${formatMoney(state.priceMin)} – ${formatMoney(state.priceMax)}`);
      }
    }

    if (durationMaxEl && state.durationMax !== toNumber(durationMaxEl.max)) {
      chips.push(formatDuration(state.durationMax));
    }

    appliedEl.innerHTML = chips.length
      ? chips.map((chip) => `<span class="rf-applied-chip">${chip}</span>`).join("")
      : `<span class="rf-empty">No filters applied</span>`;
  }

  function applyFilters() {
    collectCards();
    sortCards();

    let visible = 0;
    cards.forEach((card) => {
      const show = cardMatches(card);
      card.hidden = !show;
      if (show) visible += 1;
    });

    if (visibleCountEl) visibleCountEl.textContent = String(visible);
    if (visibleTextEl) visibleTextEl.textContent = visible === 1 ? "result" : "results";
    if (resultsCopyEl) {
      resultsCopyEl.textContent = visible
        ? `${visible} flight option${visible === 1 ? "" : "s"} sorted ${sortLabels[state.sort] || "by AI recommendation"}.`
        : "No flights match these filters yet. Try widening price, timing, baggage, or refundability.";
    }
    if (applyBtn) {
      applyBtn.textContent = visible ? `Show ${visible} result${visible === 1 ? "" : "s"}` : "No matches";
      applyBtn.disabled = visible === 0;
    }

    updateApplied();
  }

  function resetSet(setRef, buttons, keyName) {
    setRef.clear();
    buttons.forEach((btn) => btn.classList.remove("is-active"));
    if (keyName === "airlines" && airlinesEl) {
      airlinesEl.querySelectorAll("[data-airline]").forEach((input) => {
        input.checked = true;
      });
    }
    if (keyName === "airports" && airportsEl) {
      airportsEl.querySelectorAll("[data-airport]").forEach((input) => {
        input.checked = true;
      });
    }
  }

  function resetFilters() {
    resetSet(state.stops, stopButtons, "stops");
    resetSet(state.departWindows, departWindowButtons, "departWindows");
    resetSet(state.arrivalWindows, arrivalWindowButtons, "arrivalWindows");
    resetSet(state.bags, bagButtons, "bags");
    resetSet(state.refundability, refundButtons, "refundability");
    state.airlines.clear();
    state.airports.clear();
    state.priceMin = 0;
    state.priceMax = 0;
    state.durationMax = 0;
    updateRangeControls();
    renderDynamicFilters();
    applyFilters();
  }

  function attachAirportSuggestions() {
    const inputs = Array.from(document.querySelectorAll(".airport-input"));
    if (!inputs.length) return;

    const box = document.createElement("div");
    box.className = "airport-suggest";
    box.style.display = "none";
    document.body.appendChild(box);

    let activeInput = null;
    let abortCtrl = null;
    let debounceTimer = null;

    function closeBox() {
      if (debounceTimer) window.clearTimeout(debounceTimer);
      debounceTimer = null;
      try {
        abortCtrl?.abort();
      } catch (error) {}
      abortCtrl = null;
      activeInput = null;
      box.style.display = "none";
      box.innerHTML = "";
    }

    function positionBox(input) {
      const rect = input.getBoundingClientRect();
      const width = Math.min(Math.max(rect.width, 280), window.innerWidth - 24);
      const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
      box.style.left = `${left + window.scrollX}px`;
      box.style.top = `${rect.bottom + 8 + window.scrollY}px`;
      box.style.width = `${width}px`;
    }

    async function lookup(query) {
      if (abortCtrl) abortCtrl.abort();
      abortCtrl = new AbortController();
      const response = await fetch(`/airports?q=${encodeURIComponent(query)}`, { signal: abortCtrl.signal });
      if (!response.ok) return [];
      return response.json();
    }

    inputs.forEach((input) => {
      input.addEventListener("focus", () => {
        activeInput = input;
        if (box.style.display === "block") positionBox(input);
      });

      input.addEventListener("input", () => {
        const query = input.value.trim();
        activeInput = input;
        if (debounceTimer) window.clearTimeout(debounceTimer);
        if (query.length < 2) {
          closeBox();
          return;
        }
        debounceTimer = window.setTimeout(async () => {
          try {
            const items = await lookup(query);
            if (activeInput !== input) return;
            box.innerHTML = (items || [])
              .map((item) => {
                const label = String(item.label || item.code || "");
                const short = label.replace(`${item.code} — `, "");
                return `<button type="button" class="airport-item" data-value="${item.code}"><span class="airport-code">${item.code}</span><span class="airport-label">${short}</span></button>`;
              })
              .join("");
            box.style.display = items.length ? "block" : "none";
            if (items.length) positionBox(input);
          } catch (error) {
            closeBox();
          }
        }, 140);
      });
    });

    box.addEventListener("click", (event) => {
      const item = event.target.closest(".airport-item");
      if (!item || !activeInput) return;
      activeInput.value = item.getAttribute("data-value") || "";
      activeInput.dispatchEvent(new Event("change", { bubbles: true }));
      closeBox();
    });

    document.addEventListener("click", (event) => {
      if (!event.target.closest(".airport-input") && !event.target.closest(".airport-suggest")) closeBox();
    });

    window.addEventListener("resize", () => {
      if (activeInput && box.style.display === "block") positionBox(activeInput);
    });
  }

  function syncPrice() {
    if (!priceMinEl || !priceMaxEl) return;
    let min = toNumber(priceMinEl.value, 0);
    let max = toNumber(priceMaxEl.value, 0);
    if (min > max) {
      if (document.activeElement === priceMinEl) {
        max = min;
        priceMaxEl.value = String(max);
      } else {
        min = max;
        priceMinEl.value = String(min);
      }
    }
    state.priceMin = min;
    state.priceMax = max;
    if (priceLabelEl) priceLabelEl.textContent = `${formatMoney(min)} – ${formatMoney(max)}`;
    applyFilters();
  }

  function toggleSetButton(setRef, value, button) {
    if (setRef.has(value)) {
      setRef.delete(value);
      button.classList.remove("is-active");
    } else {
      setRef.add(value);
      button.classList.add("is-active");
    }
    applyFilters();
  }

  function updateCardDetailsToggle(button, open) {
    const detailsId = button.getAttribute("aria-controls");
    if (!detailsId) return;
    const details = document.getElementById(detailsId);
    if (!details) return;
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.setAttribute("aria-label", open ? "Hide details" : "Show details");
    details.hidden = !open;
  }

  function syncSavedButtons() {
    cardsEl.querySelectorAll("[data-save-id]").forEach((button) => {
      const id = button.getAttribute("data-save-id") || "";
      const saved = savedFlights.has(id);
      button.classList.toggle("is-saved", saved);
      button.setAttribute("aria-pressed", saved ? "true" : "false");
      button.setAttribute("aria-label", saved ? "Remove saved flight" : "Save flight");
      button.textContent = saved ? "♥" : "♡";
    });
  }

  function attachEvents() {
    editToggle?.addEventListener("click", () => {
      const hidden = editBar.hasAttribute("hidden");
      if (hidden) editBar.removeAttribute("hidden");
      else editBar.setAttribute("hidden", "hidden");
    });

    tripButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        if (!tripTypeInput) return;
        tripTypeInput.value = btn.dataset.tripType || "roundtrip";
        updateTripTypeUi();
      });
    });

    swapBtn?.addEventListener("click", () => {
      if (!originInput || !destinationInput) return;
      const temp = originInput.value;
      originInput.value = destinationInput.value;
      destinationInput.value = temp;
    });

    editForm?.addEventListener("submit", (event) => {
      if (!originInput || !destinationInput || !departInput) return;
      if (originInput.value.trim().toUpperCase() === destinationInput.value.trim().toUpperCase()) {
        event.preventDefault();
        destinationInput.setCustomValidity("Origin and destination cannot be the same.");
        destinationInput.reportValidity();
        destinationInput.focus();
        window.setTimeout(() => destinationInput.setCustomValidity(""), 50);
        return;
      }
      if (tripTypeInput?.value !== "oneway" && returnInput?.value && returnInput.value < departInput.value) {
        event.preventDefault();
        returnInput.setCustomValidity("Return date must be the same day or after departure.");
        returnInput.reportValidity();
        returnInput.focus();
        window.setTimeout(() => returnInput.setCustomValidity(""), 50);
      }
    });

    const openDatePicker = () => {
      if (!departDisplayInput) return;
      departDisplayInput.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      departDisplayInput.focus();
    };

    dateRangeButton?.addEventListener("click", openDatePicker);

    departDisplayInput?.addEventListener("input", updateDateRangeSummary);
    departDisplayInput?.addEventListener("change", updateDateRangeSummary);
    returnDisplayInput?.addEventListener("input", updateDateRangeSummary);
    returnDisplayInput?.addEventListener("change", updateDateRangeSummary);
    departInput?.addEventListener("input", updateDateRangeSummary);
    departInput?.addEventListener("change", updateDateRangeSummary);
    returnInput?.addEventListener("input", updateDateRangeSummary);
    returnInput?.addEventListener("change", updateDateRangeSummary);

    filterToggle?.addEventListener("click", () => setSidebarOpen(true));
    filterBackdrop?.addEventListener("click", () => setSidebarOpen(false));
    sidebarClose?.addEventListener("click", () => setSidebarOpen(false));
    applyBtn?.addEventListener("click", () => setSidebarOpen(false));
    clearBtn?.addEventListener("click", resetFilters);
    clearLinkBtn?.addEventListener("click", resetFilters);

    sortButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        state.sort = btn.dataset.sort || "recommended";
        updateSortInputs();
        if (sortDropdown && sortTrigger) {
          sortDropdown.hidden = true;
          sortTrigger.setAttribute("aria-expanded", "false");
        }
        applyFilters();
      });
    });

    sortTrigger?.addEventListener("click", () => {
      if (!sortDropdown) return;
      const open = sortDropdown.hidden;
      sortDropdown.hidden = !open;
      sortTrigger.setAttribute("aria-expanded", open ? "true" : "false");
    });

    document.addEventListener("click", (event) => {
      if (!sortMenu) return;
      if (sortMenu.contains(event.target)) return;
      if (sortDropdown && sortTrigger) {
        sortDropdown.hidden = true;
        sortTrigger.setAttribute("aria-expanded", "false");
      }
    });

    stopButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const value = btn.dataset.stop;
        if (!value) return;
        toggleSetButton(state.stops, value, btn);
      });
    });

    departWindowButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const value = btn.dataset.departWindow;
        if (!value) return;
        toggleSetButton(state.departWindows, value, btn);
      });
    });

    arrivalWindowButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const value = btn.dataset.arrivalWindow;
        if (!value) return;
        toggleSetButton(state.arrivalWindows, value, btn);
      });
    });

    bagButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const value = btn.dataset.bag;
        if (!value) return;
        toggleSetButton(state.bags, value, btn);
      });
    });

    refundButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const value = btn.dataset.refundability;
        if (!value) return;
        toggleSetButton(state.refundability, value, btn);
      });
    });

    priceMinEl?.addEventListener("input", syncPrice);
    priceMaxEl?.addEventListener("input", syncPrice);

    durationMaxEl?.addEventListener("input", () => {
      state.durationMax = toNumber(durationMaxEl.value, 0);
      if (durationLabelEl) {
        durationLabelEl.textContent = state.durationMax >= toNumber(durationMaxEl.max, 0) ? "Any duration" : formatDuration(state.durationMax);
      }
      applyFilters();
    });

    airlinesEl?.addEventListener("change", () => {
      state.airlines.clear();
      const all = Array.from(airlinesEl.querySelectorAll("[data-airline]"));
      const checked = all.filter((input) => input.checked);
      if (checked.length !== all.length) {
        checked.forEach((input) => {
          const value = input.getAttribute("data-airline");
          if (value) state.airlines.add(value);
        });
      }
      applyFilters();
    });

    airportsEl?.addEventListener("change", () => {
      state.airports.clear();
      const all = Array.from(airportsEl.querySelectorAll("[data-airport]"));
      const checked = all.filter((input) => input.checked);
      if (checked.length !== all.length) {
        checked.forEach((input) => {
          const value = input.getAttribute("data-airport");
          if (value) state.airports.add(value);
        });
      }
      applyFilters();
    });

    cardsEl.addEventListener("click", (event) => {
      const saveButton = event.target.closest("[data-save-id]");
      if (saveButton) {
        const id = saveButton.getAttribute("data-save-id") || "";
        if (!id) return;
        if (savedFlights.has(id)) savedFlights.delete(id);
        else savedFlights.add(id);
        persistSavedFlights();
        syncSavedButtons();
        return;
      }

      const detailToggle = event.target.closest("[data-detail-toggle]");
      if (detailToggle) {
        const isOpen = detailToggle.getAttribute("aria-expanded") === "true";
        updateCardDetailsToggle(detailToggle, !isOpen);
        return;
      }

      const tab = event.target.closest("[data-tab-target]");
      if (!tab) return;
      const details = tab.closest(".rf-details");
      if (!details) return;
      const target = tab.dataset.tabTarget;
      details.querySelectorAll("[data-tab-target]").forEach((btn) => {
        btn.classList.toggle("is-active", btn === tab);
      });
      details.querySelectorAll("[data-tab-panel]").forEach((panel) => {
        const open = panel.dataset.tabPanel === target;
        panel.classList.toggle("is-active", open);
        panel.hidden = !open;
      });
    });
  }

  function observeResults() {
    const observer = new MutationObserver(() => {
      const next = Array.from(cardsEl.querySelectorAll(".rf-card")).filter((card) => !card.classList.contains("flex-live-skeleton"));
      if (next.length === cards.length) return;
      collectCards();
      updateRangeControls();
      renderDynamicFilters();
      applyFilters();
    });
    observer.observe(cardsEl, { childList: true, subtree: false });
  }

  loadSavedFlights();
  collectCards();
  updateTripTypeUi();
  updateRangeControls();
  renderDynamicFilters();
  updateSortInputs();
  updateDateRangeSummary();
  attachAirportSuggestions();
  attachEvents();
  observeResults();
  applyFilters();
})();
