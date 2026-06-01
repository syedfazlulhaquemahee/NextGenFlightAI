(function () {
  const stateNode = document.getElementById("checkoutClientState");
  const form = document.getElementById("seatSelectionForm");
  if (!stateNode || !form) return;

  let state = {};
  try {
    state = JSON.parse(stateNode.textContent || "{}");
  } catch (_err) {
    state = {};
  }

  const tablist = document.querySelector(".seat-route-tabs");
  const tabs = Array.from(document.querySelectorAll("[data-seat-tab]"));
  const tabStatusEls = Array.from(document.querySelectorAll("[data-seat-tab-status]"));
  const mapRoot = form.querySelector("[data-seat-map-root]");
  const totalEls = document.querySelectorAll("[data-total-display]");
  const ancillaryTotalEl = document.querySelector("[data-ancillary-total]");
  const hiddenAncillaries = document.getElementById("ancillariesPayload");
  const flowHint = document.getElementById("seatFlowHint");
  const serviceCatalog = Array.isArray(state.offer_services) ? state.offer_services : [];
  const serviceById = new Map(serviceCatalog.map((service) => [service.id, service]));
  const baseTotal = Number.parseFloat(state.base_total || state.initial_total || "0") || 0;
  const travelerCount = Math.max(
    1,
    Number.parseInt(String(state.seat_traveler_count || "0"), 10) ||
      document.querySelectorAll("[data-seat-status-for]").length ||
      1,
  );

  /** segment id string -> { "0": { designator, serviceId }, ... } */
  const assignments = {};

  function formatMoney(amount) {
    return amount.toFixed(2);
  }

  function normalizeDesignator(value) {
    return String(value || "").trim().toUpperCase();
  }

  function activeSegId() {
    const tab = tabs.find((t) => t.classList.contains("is-active"));
    return tab ? String(tab.dataset.seatTab) : "0";
  }

  function allPanels() {
    return mapRoot ? Array.from(mapRoot.querySelectorAll("[data-seat-panel]")) : [];
  }

  function applySeatScaleForPanel(panel) {
    if (!panel) return;
    const rows = Array.from(panel.querySelectorAll(".seat-row-cells"));
    if (!rows.length) return;
    const shell = panel.querySelector(".seat-map-scroll");
    if (!shell) return;
    const setScale = (tile) => {
      const aisle = Math.max(10, Math.round(tile * 0.7));
      panel.style.setProperty("--seat-tile-w", `${tile}px`);
      panel.style.setProperty("--seat-aisle-w", `${aisle}px`);
    };
    setScale(38);
    for (let tile = 38; tile >= 18; tile -= 1) {
      setScale(tile);
      if (shell.scrollWidth <= shell.clientWidth + 1) break;
    }
  }

  function activateSeatTab(tab) {
    if (!tab || !tabs.includes(tab)) return;
    const index = String(tab.dataset.seatTab);
    tabs.forEach((item) => {
      const isActive = item === tab;
      item.classList.toggle("is-active", isActive);
      item.setAttribute("aria-selected", isActive ? "true" : "false");
      item.setAttribute("tabindex", isActive ? "0" : "-1");
    });
    allPanels().forEach((panel) => {
      const hidden = String(panel.dataset.seatPanel) !== index;
      panel.classList.toggle("is-hidden", hidden);
      panel.setAttribute("aria-hidden", hidden ? "true" : "false");
    });
    const activePanel = mapRoot?.querySelector(`[data-seat-panel="${index}"]`);
    applySeatScaleForPanel(activePanel);
    updateTravelerStatusForSeg(index);
  }

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => activateSeatTab(tab));
  });

  if (tablist && tabs.length) {
    tablist.addEventListener("keydown", (event) => {
      const activeIndex = tabs.findIndex((t) => t.classList.contains("is-active"));
      if (activeIndex < 0) return;
      let nextIndex = activeIndex;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        nextIndex = (activeIndex + 1) % tabs.length;
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        nextIndex = (activeIndex - 1 + tabs.length) % tabs.length;
      } else if (event.key === "Home") {
        nextIndex = 0;
      } else if (event.key === "End") {
        nextIndex = tabs.length - 1;
      } else {
        return;
      }
      event.preventDefault();
      activateSeatTab(tabs[nextIndex]);
      tabs[nextIndex].focus();
    });
  }

  function firstEmptySlot(a) {
    for (let i = 0; i < travelerCount; i++) {
      if (!a[String(i)]) return i;
    }
    return null;
  }

  function allSlotsFilled(a) {
    for (let i = 0; i < travelerCount; i++) {
      if (!a[String(i)]) return false;
    }
    return true;
  }

  function parsePassengerServices(btn) {
    try {
      const raw =
        btn.getAttribute("data-passenger-services") || btn.dataset.passengerServices || "[]";
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_err) {
      return [];
    }
  }

  function serviceRowForSlot(psvc, slotIndex) {
    return psvc.find((p) => Number(p.passenger_index) === slotIndex);
  }

  function flashValue(el) {
    if (!el) return;
    el.classList.remove("is-updated");
    window.requestAnimationFrame(() => {
      el.classList.add("is-updated");
      window.setTimeout(() => el.classList.remove("is-updated"), 220);
    });
  }

  function selectedServiceObjects() {
    const ids = [];
    Object.keys(assignments).forEach((seg) => {
      const a = assignments[seg];
      for (let i = 0; i < travelerCount; i++) {
        const slot = a[String(i)];
        if (slot && slot.serviceId) ids.push(slot.serviceId);
      }
    });
    return ids.map((id) => serviceById.get(id)).filter(Boolean);
  }

  function updateTotalsAndPayload() {
    const services = selectedServiceObjects();
    const ancillaryTotal = services.reduce((sum, service) => sum + (Number.parseFloat(service.amount || "0") || 0), 0);
    const total = baseTotal + ancillaryTotal;

    totalEls.forEach((el) => {
      el.textContent = formatMoney(total);
      flashValue(el);
    });
    if (ancillaryTotalEl) {
      ancillaryTotalEl.textContent = formatMoney(ancillaryTotal);
      flashValue(ancillaryTotalEl);
    }

    if (hiddenAncillaries) {
      hiddenAncillaries.value = JSON.stringify({
        selected_services: services,
        services: services.map((service) => ({ id: service.id, quantity: 1 })),
      });
    }
  }

  function clearAllTileVisuals() {
    form.querySelectorAll(".seat-pick").forEach((btn) => {
      for (let i = 0; i < 12; i++) btn.classList.remove(`has-pax-${i}`);
      btn.querySelectorAll(".seat-tile-badge--pax").forEach((badge) => badge.classList.remove("is-lit"));
    });
  }

  function findSeatButton(panel, designator) {
    const want = normalizeDesignator(designator);
    return Array.from(panel.querySelectorAll(".seat-pick")).find((b) => normalizeDesignator(b.dataset.seatDesignator) === want) || null;
  }

  function syncTileVisuals() {
    clearAllTileVisuals();
    Object.keys(assignments).forEach((seg) => {
      const panel = mapRoot?.querySelector(`[data-seat-panel="${seg}"]`);
      if (!panel) return;
      const a = assignments[seg];
      for (let i = 0; i < travelerCount; i++) {
        const slot = a[String(i)];
        if (!slot) continue;
        const btn = findSeatButton(panel, slot.designator);
        if (!btn) continue;
        btn.classList.add(`has-pax-${i}`);
        const badge = btn.querySelector(`[data-pax-badge="${i}"]`);
        if (badge) badge.classList.add("is-lit");
      }
    });
  }

  function updateTabStatuses() {
    tabStatusEls.forEach((statusEl) => {
      const segIdx = statusEl.dataset.seatTabStatus;
      const a = assignments[segIdx] || {};
      const parts = [];
      for (let i = 0; i < travelerCount; i++) {
        const slot = a[String(i)];
        if (slot) parts.push(`T${i + 1}: ${slot.designator}`);
      }
      statusEl.textContent = parts.length ? parts.join(" · ") : "No seat selected";
      const tab = tabs.find((item) => item.dataset.seatTab === segIdx);
      if (tab) tab.classList.toggle("has-selection", parts.length > 0);
    });
  }

  function updateTravelerStatusForSeg(seg) {
    const a = assignments[seg] || {};
    for (let i = 0; i < travelerCount; i++) {
      const el = document.querySelector(`[data-seat-status-for="${i}"]`);
      if (!el) continue;
      const slot = a[String(i)];
      el.textContent = slot ? slot.designator : "—";
    }
    updateFlowHint(seg);
  }

  function updateFlowHint(seg) {
    if (!flowHint) return;
    const a = assignments[seg] || {};
    const segNum = Number.parseInt(seg, 10) || 0;
    const lastSeg = tabs.length ? Number.parseInt(String(tabs[tabs.length - 1].dataset.seatTab), 10) || 0 : 0;

    if (allSlotsFilled(a)) {
      if (segNum < lastSeg) {
        flowHint.textContent = "Everyone has a seat on this flight. Choose seats for the next flight above.";
      } else {
        flowHint.textContent = "Everyone has a seat on every flight. Continue to checkout when ready.";
      }
      return;
    }
    const next = firstEmptySlot(a);
    if (next === 0) {
      flowHint.textContent = "Tap a seat for Traveler 1. (Tap it again to clear.)";
    } else {
      flowHint.textContent = `Tap a seat for Traveler ${next + 1}. (Tap their seat again to clear.)`;
    }
  }

  function syncAll() {
    syncTileVisuals();
    updateTabStatuses();
    updateTravelerStatusForSeg(activeSegId());
    updateTotalsAndPayload();
  }

  function handleSeatPick(btn) {
    if (!mapRoot || btn.disabled) return;
    const panel = btn.closest("[data-seat-panel]");
    if (!panel) return;
    const seg = String(panel.dataset.seatPanel);
    const designator = normalizeDesignator(btn.dataset.seatDesignator);
    const psvc = parsePassengerServices(btn);
    if (!psvc.length) return;

    const a = assignments[seg] || (assignments[seg] = {});

    for (let i = 0; i < travelerCount; i++) {
      const slot = a[String(i)];
      if (slot && normalizeDesignator(slot.designator) === designator) {
        delete a[String(i)];
        syncAll();
        return;
      }
    }

    let slot = firstEmptySlot(a);
    if (slot === null) {
      delete a[String(travelerCount - 1)];
      slot = travelerCount - 1;
    }

    const row = serviceRowForSlot(psvc, slot);
    if (!row || !row.service_id) return;

    a[String(slot)] = { designator: btn.dataset.seatDesignator, serviceId: row.service_id };

    if (allSlotsFilled(a)) {
      const segNum = Number.parseInt(seg, 10) || 0;
      const next = tabs.find((t) => Number.parseInt(String(t.dataset.seatTab), 10) === segNum + 1);
      if (next) activateSeatTab(next);
    }
    syncAll();
  }

  if (mapRoot) {
    mapRoot.addEventListener("click", (event) => {
      const btn = event.target.closest(".seat-pick");
      if (!btn) return;
      handleSeatPick(btn);
    });
  }

  window.addEventListener("resize", () => {
    const panel = mapRoot?.querySelector(`[data-seat-panel="${activeSegId()}"]`);
    applySeatScaleForPanel(panel);
  });

  syncAll();
  allPanels().forEach((panel) => applySeatScaleForPanel(panel));
  updateTravelerStatusForSeg(activeSegId());
})();
