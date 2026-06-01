(function () {
  const embedNode = document.getElementById("duffelAncillariesEmbed");
  const stateNode = document.getElementById("checkoutClientState");
  const form = document.getElementById("duffelAncillariesForm");
  const slot = document.getElementById("duffelAncillariesSlot");
  const host = document.querySelector("duffel-ancillaries");
  const hiddenAncillaries = document.getElementById("ancillariesPayload");
  const totalEls = document.querySelectorAll("[data-total-display]");
  const ancillaryTotalEl = document.querySelector("[data-ancillary-total]");

  if (!embedNode || !form || !host || !hiddenAncillaries) return;

  let embed = {};
  let state = {};
  try {
    embed = JSON.parse(embedNode.textContent || "{}");
  } catch (_e) {
    embed = {};
  }
  try {
    state = JSON.parse(stateNode ? stateNode.textContent || "{}" : "{}");
  } catch (_e) {
    state = {};
  }

  const currency = state.currency || "USD";
  const ticketTotal = Number.parseFloat(state.base_total || state.initial_total || "0") || 0;
  const serviceCatalog = Array.isArray(state.offer_services) ? state.offer_services : [];
  const serviceById = new Map(serviceCatalog.map((s) => [s.id, s]));

  function formatMoney(amount) {
    return amount.toFixed(2);
  }

  function flashValue(el) {
    if (!el) return;
    el.classList.remove("is-updated");
    window.requestAnimationFrame(() => {
      el.classList.add("is-updated");
      window.setTimeout(() => el.classList.remove("is-updated"), 220);
    });
  }

  function amountFromDuffelService(svc) {
    if (!svc) return "0.00";
    const direct = svc.total_amount ?? svc.amount;
    if (direct != null && direct !== "") return String(direct);
    const inf = svc.serviceInformation;
    if (inf && (inf.total_amount != null || inf.amount != null)) {
      return String(inf.total_amount ?? inf.amount ?? "0.00");
    }
    return "0.00";
  }

  function labelFromDuffelService(svc) {
    if (!svc) return "Extra";
    const inf = svc.serviceInformation;
    if (inf) {
      if (inf.designator) return `Seat ${inf.designator}`;
      if (inf.passengerName) return String(inf.passengerName);
    }
    return String(svc.name || svc.description || "Extra").trim() || "Extra";
  }

  function passengerIdFromServiceInfo(inf) {
    if (!inf) return "";
    return String(inf.passenger_id || inf.passengerId || "").trim();
  }

  function updateTravelerSeatLines(metadata) {
    const block = (metadata && metadata.seat_services) || [];
    const byPid = new Map();
    block.forEach((svc) => {
      const inf = svc.serviceInformation;
      if (!inf) return;
      const pid = passengerIdFromServiceInfo(inf);
      if (!pid) return;
      const des = String(inf.designator || "").trim();
      if (!des) return;
      if (!byPid.has(pid)) byPid.set(pid, []);
      byPid.get(pid).push(des);
    });

    document.querySelectorAll(".seat-traveler-status[data-passenger-id]").forEach((row) => {
      const pid = row.getAttribute("data-passenger-id") || "";
      const line = row.querySelector("[data-traveler-seat-line]");
      if (!line) return;
      const seats = pid ? byPid.get(pid) || [] : [];
      const uniq = [...new Set(seats)];
      if (!uniq.length) {
        line.textContent = "";
        line.hidden = true;
        return;
      }
      line.hidden = false;
      line.textContent = uniq.length > 1 ? `Seats: ${uniq.join(", ")}` : `Seat: ${uniq[0]}`;
    });

    /* Fallback: Duffel sometimes omits passenger_id on seat metadata — show seats on the only traveler row. */
    if (!byPid.size && block.length) {
      const all = [];
      block.forEach((svc) => {
        const inf = svc.serviceInformation;
        const des = inf && String(inf.designator || "").trim();
        if (des) all.push(des);
      });
      const uniq = [...new Set(all)];
      const lines = document.querySelectorAll(".seat-traveler-status [data-traveler-seat-line]");
      if (uniq.length && lines.length === 1) {
        const line = lines[0];
        line.hidden = false;
        line.textContent = uniq.length > 1 ? `Seats: ${uniq.join(", ")}` : `Seat: ${uniq[0]}`;
      }
    }
  }

  function rowsFromMetadata(metadata) {
    const rows = [];
    const blocks = [
      metadata.baggage_services,
      metadata.seat_services,
      metadata.cancel_for_any_reason_services,
    ];
    blocks.forEach((block) => {
      if (!Array.isArray(block)) return;
      block.forEach((svc) => {
        if (!svc || !svc.id) return;
        const amt = amountFromDuffelService(svc);
        const inf = svc.serviceInformation;
        rows.push({
          id: svc.id,
          quantity: svc.quantity || 1,
          amount: String(amt),
          label: labelFromDuffelService(svc),
          currency: (inf && (inf.total_currency || inf.currency)) || svc.total_currency || svc.currency || currency,
        });
      });
    });
    return rows;
  }

  function rowsFromOrderData(data) {
    const services = Array.isArray(data.services) ? data.services : [];
    if (!services.length) return [];
    return services.map((s) => {
      const cat = serviceById.get(s.id);
      const parsed = amountFromDuffelService(s);
      const hasParsed = parsed && parsed !== "0.00" && parsed !== "0";
      const amt = hasParsed ? parsed : (cat && cat.amount) || "0.00";
      return {
        id: s.id,
        quantity: s.quantity || 1,
        amount: String(amt),
        label: (cat && cat.label) || labelFromDuffelService(s),
        currency: (cat && cat.currency) || currency,
      };
    });
  }

  function applyPayload(data, metadata) {
    const services = Array.isArray(data.services) ? data.services : [];
    const payment = Array.isArray(data.payments) && data.payments[0] ? data.payments[0] : null;
    const paymentTotal = payment && payment.amount != null ? Number.parseFloat(String(payment.amount)) : NaN;
    let selectedRows = rowsFromMetadata(metadata || {});
    if (!selectedRows.length) {
      selectedRows = rowsFromOrderData(data);
    }

    let ancillaryTotal = selectedRows.reduce(
      (sum, row) => sum + (Number.parseFloat(row.amount || "0") || 0),
      0,
    );
    let displayTotal = ticketTotal + ancillaryTotal;
    if (!Number.isNaN(paymentTotal)) {
      displayTotal = paymentTotal;
      ancillaryTotal = Math.max(0, paymentTotal - ticketTotal);
    }

    totalEls.forEach((el) => {
      el.textContent = formatMoney(displayTotal);
      flashValue(el);
    });
    if (ancillaryTotalEl) {
      ancillaryTotalEl.textContent = formatMoney(ancillaryTotal);
      flashValue(ancillaryTotalEl);
    }

    const payloadServices = services.map((s) => ({
      id: s.id,
      quantity: s.quantity || 1,
      amount: s.total_amount != null ? String(s.total_amount) : s.amount != null ? String(s.amount) : undefined,
    }));

    hiddenAncillaries.value = JSON.stringify({
      services: payloadServices,
      selected_services: selectedRows.length ? selectedRows : payloadServices,
    });

    updateTravelerSeatLines(metadata || {});
  }

  /**
   * Move Duffel’s surface to document.body and pin it to the grid slot’s box so modals aren’t
   * clipped or shifted by ancestor overflow / transforms (e.g. .review-card:hover).
   * Height is derived from the surface’s own content so it never overflows into sections below.
   */
  function portalDuffelSurfaceToViewport() {
    if (!slot) return;
    const surface = slot.querySelector(".duffel-ancillaries-surface");
    if (!surface || surface.dataset.ngfPortaled === "1") return;

    const position = () => {
      const r = slot.getBoundingClientRect();
      // Hide when the slot has scrolled fully out of the viewport so it doesn't cover content below.
      const slotVisible = r.bottom > 64 && r.top < window.innerHeight;
      if (!slotVisible) {
        surface.style.display = "none";
        return;
      }
      surface.style.display = "";
      const pad = 6;
      const viewTop = Math.max(64, r.top);
      const left = Math.max(pad, r.left);
      const width = Math.max(280, r.width);
      // Cap height at viewport space below the surface top so it never covers content below.
      const maxAvail = Math.max(200, window.innerHeight - viewTop - 16);
      const contentH = Math.max(200, surface.scrollHeight || 0);
      const height = Math.min(contentH, maxAvail);
      // Sync the slot placeholder so normal-flow content below is pushed down correctly.
      slot.style.minHeight = `${height}px`;
      surface.style.cssText = [
        "position:fixed",
        `left:${left}px`,
        `top:${viewTop}px`,
        `width:${width}px`,
        `height:${height}px`,
        "overflow-y:auto",
        "overflow-x:hidden",
        "z-index:20050",
        "margin:0",
        "pointer-events:auto",
      ].join(";");
    };

    document.body.appendChild(surface);
    surface.dataset.ngfPortaled = "1";

    // Defer first position so the surface has a rendered scrollHeight.
    window.requestAnimationFrame(() => {
      position();
      // Re-check after Duffel’s async render passes settle.
      window.setTimeout(position, 300);
      window.setTimeout(position, 900);
    });

    // Observe both slot (layout shifts) and surface (content-height changes from Duffel renders).
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(position) : null;
    if (ro) { ro.observe(slot); ro.observe(surface); }
    window.addEventListener("resize", position, { passive: true });
    window.addEventListener("scroll", position, { capture: true, passive: true });
  }

  function isDarkMode() {
    return document.documentElement.getAttribute("data-theme") !== "light";
  }

  function buildShadowCss(dark) {
    if (!dark) return ":host { overflow:visible !important; }";
    return `
:host { overflow:visible !important; color-scheme:dark; }
/* Force ALL block elements dark so Duffel's hardcoded white card backgrounds are overridden */
:host div,:host li,:host ul,:host ol,:host section,:host article,:host aside,:host header,:host footer {
  background-color: rgb(18,30,48) !important;
  border-color: rgba(146,181,228,0.2) !important;
}
/* All text white by default */
:host * { color: #edf3fb !important; }
/* Secondary / description text slightly muted */
:host p { color: rgba(168,200,238,0.78) !important; }
/* Buttons: transparent bg, icon colour */
:host button { background-color: transparent !important; }
/* 'Not available' badge */
:host span[class] {
  background-color: rgba(30,50,78,0.85) !important;
  color: rgba(168,200,238,0.85) !important;
  border-color: rgba(146,181,228,0.2) !important;
}
/* SVG icons */
:host svg path,:host svg circle,:host svg rect,:host svg polyline,:host svg line {
  stroke: rgba(168,200,238,0.8) !important;
  fill: none !important;
}
    `.trim();
  }

  function injectShadowOverflowVisible() {
    const sr = host.shadowRoot;
    if (!sr || sr.querySelector("style[data-ngf-shadow-tune]")) return;
    const st = document.createElement("style");
    st.dataset.ngfShadowTune = "1";
    st.textContent = buildShadowCss(isDarkMode());
    sr.appendChild(st);

    /* Keep our style last — Duffel appends its own styles as it renders;
       every time it does we move ours back to the end so we always win. */
    const shadowChildObs = new MutationObserver(() => {
      if (st.parentNode === sr && st !== sr.lastElementChild) sr.appendChild(st);
    });
    shadowChildObs.observe(sr, { childList: true });

    /* Re-theme when user toggles dark/light */
    const themeObs = new MutationObserver(() => {
      st.textContent = buildShadowCss(isDarkMode());
      if (st.parentNode === sr) sr.appendChild(st);
    });
    themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }

  function mount() {
    if (typeof host.render !== "function") {
      window.setTimeout(mount, 40);
      return;
    }
    host.addEventListener("onPayloadReady", (event) => {
      const detail = event.detail || {};
      applyPayload(detail.data || {}, detail.metadata || {});
    });
    host.render({
      debug: false,
      offer: embed.offer,
      seat_maps: embed.seat_maps,
      passengers: embed.passengers,
      services: embed.services,
      styles: embed.styles,
    });

    window.requestAnimationFrame(() => {
      portalDuffelSurfaceToViewport();
      window.setTimeout(injectShadowOverflowVisible, 0);
      window.setTimeout(injectShadowOverflowVisible, 400);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
