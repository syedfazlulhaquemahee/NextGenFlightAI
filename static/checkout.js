(function () {
  const stateNode = document.getElementById("checkoutClientState");
  const form = document.getElementById("premiumCheckoutForm");
  if (!stateNode || !form) return;

  let state = {};
  try {
    state = JSON.parse(stateNode.textContent || "{}");
  } catch (_err) {
    state = {};
  }

  const totalEls = document.querySelectorAll("[data-total-display]");
  const ancillaryTotalEl = document.querySelector("[data-ancillary-total]");
  const selectedExtrasList = document.querySelector("[data-selected-extras-list]");
  const hiddenAncillaries = document.getElementById("ancillariesPayload");
  const checkboxes = Array.from(document.querySelectorAll(".ancillary-checkbox"));
  const travelerJumpButtons = Array.from(document.querySelectorAll("[data-traveler-jump]"));
  const progressModal = document.querySelector("[data-progress-modal]");
  const expiryBar = document.querySelector("[data-expiry-bar]");
  const expiryCountdown = document.querySelector("[data-expiry-countdown]");
  const mobilePricePin = document.querySelector("[data-mobile-price-pin]");
  const priceSummaryCard = document.querySelector(".checkout-price-panel");

  const currency = state.currency || "USD";
  const baseTotal = Number.parseFloat(state.base_total || state.initial_total || "0") || 0;
  const serviceCatalog = Array.isArray(state.offer_services) ? state.offer_services : [];
  const serviceById = new Map(serviceCatalog.map((service) => [service.id, service]));
  const stateSelected = Array.isArray(state.selected_services) ? state.selected_services : [];

  function formatMoney(amount) {
    return amount.toFixed(2);
  }

  /** Seats from the seat map (esp. multi-passenger) are not checkbox-backed; merge them with optional bag toggles. */
  function selectedServices() {
    const byId = new Map();
    stateSelected.forEach((svc) => {
      if (svc && svc.id) {
        byId.set(svc.id, serviceById.get(svc.id) || svc);
      }
    });
    checkboxes.forEach((input) => {
      const sid = input.dataset.serviceId;
      if (!sid) return;
      if (input.checked) {
        const entry = serviceById.get(sid);
        if (entry) byId.set(sid, entry);
      } else if (!input.disabled) {
        byId.delete(sid);
      }
    });
    return [...byId.values()];
  }

  function updateAncillaries() {
    const services = selectedServices();
    const ancillaryTotal = services.reduce((sum, service) => sum + (Number.parseFloat(service.amount || "0") || 0), 0);
    const total = baseTotal + ancillaryTotal;

    totalEls.forEach((el) => {
      el.textContent = formatMoney(total);
    });
    if (ancillaryTotalEl) ancillaryTotalEl.textContent = formatMoney(ancillaryTotal);

    if (selectedExtrasList) {
      if (!services.length) {
        selectedExtrasList.innerHTML = '<div class="checkout-quiet checkout-quiet--inline">None</div>';
      } else {
        selectedExtrasList.innerHTML = services
          .map((service) => `<div class="selected-extra-item"><span>${service.label}</span><strong>${currency} ${formatMoney(Number.parseFloat(service.amount || "0") || 0)}</strong></div>`)
          .join("");
      }
    }

    if (hiddenAncillaries) {
      hiddenAncillaries.value = JSON.stringify({
        selected_services: services,
        services: services.map((service) => ({ id: service.id, quantity: 1 })),
      });
    }
  }

  function updateCountdown() {
    if (!expiryBar || !expiryCountdown || !state.expires_at) return;
    const label = expiryBar.querySelector(".summary-expiry-label");
    const target = new Date(state.expires_at).getTime();
    if (!target) return;
    const diffMs = target - Date.now();
    if (diffMs <= 0) {
      if (label) label.textContent = "";
      expiryCountdown.textContent = "Price expired";
      expiryBar.classList.add("is-warning");
      return;
    }
    const minutes = Math.floor(diffMs / 60000);
    const seconds = Math.floor((diffMs % 60000) / 1000);
    expiryCountdown.textContent = `${minutes}m ${String(seconds).padStart(2, "0")}s`;
    expiryBar.classList.toggle("is-warning", diffMs < 10 * 60 * 1000);
  }

  function initMobilePricePin() {
    if (!mobilePricePin || !priceSummaryCard) return;

    const isMobile = () => window.matchMedia("(max-width: 760px)").matches;
    const setVisible = (visible) => {
      if (!isMobile()) {
        mobilePricePin.hidden = true;
        mobilePricePin.classList.remove("is-visible");
        return;
      }
      mobilePricePin.hidden = false;
      mobilePricePin.classList.toggle("is-visible", visible);
    };

    const fallback = () => {
      const rect = priceSummaryCard.getBoundingClientRect();
      setVisible(rect.bottom <= 0);
    };

    if ("IntersectionObserver" in window) {
      const observer = new IntersectionObserver(
        ([entry]) => setVisible(!entry.isIntersecting && priceSummaryCard.getBoundingClientRect().bottom <= 0),
        { threshold: 0 },
      );
      observer.observe(priceSummaryCard);
      window.addEventListener("resize", fallback, { passive: true });
      fallback();
      return;
    }

    window.addEventListener("scroll", fallback, { passive: true });
    window.addEventListener("resize", fallback, { passive: true });
    fallback();
  }

  checkboxes.forEach((input) => input.addEventListener("change", updateAncillaries));
  travelerJumpButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.getElementById(`traveler-${button.dataset.travelerJump}`);
      if (!target) return;
      target.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  form.addEventListener("submit", (event) => {
    const submitter = event.submitter;
    const isFinalSubmit = !!(submitter && submitter.matches("[data-final-submit='true']"));
    if (!isFinalSubmit) return;
    if (!form.checkValidity()) return;
    if (progressModal) {
      progressModal.hidden = false;
      startBookingAnimation(progressModal);
    }
  });

  updateAncillaries();
  updateCountdown();
  initMobilePricePin();
  window.setInterval(updateCountdown, 1000);
})();

function startBookingAnimation(modal) {
  const steps = Array.from(modal.querySelectorAll(".bpm-step"));
  const fill  = modal.querySelector("#bpmFill");
  const title = modal.querySelector("#bpmTitle");
  if (!steps.length) return;

  const titles = [
    "Securing your trip",
    "Verifying details",
    "Locking in your fare",
    "Processing payment",
    "Almost there…",
  ];
  // Progress bar widths per step (0-indexed, value when step becomes active)
  const fillWidths = ["12%","30%","52%","72%","90%"];
  let current = 0;

  function activateStep(i) {
    steps.forEach((s, idx) => {
      s.classList.remove("is-active", "is-done");
      if (idx < i)  s.classList.add("is-done");
      if (idx === i) s.classList.add("is-active");
    });
    if (fill) fill.style.width = fillWidths[i] || "90%";
    if (title && titles[i]) title.textContent = titles[i];
  }

  activateStep(0);

  // Advance a step every ~1.8s — purely cosmetic, booking happens server-side
  const interval = setInterval(() => {
    current++;
    if (current >= steps.length) {
      clearInterval(interval);
      return;
    }
    activateStep(current);
  }, 1800);
}
