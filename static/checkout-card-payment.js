(function () {
  "use strict";

  const DUFFEL_VERSION = "3.16.1";

  /* ── DOM refs ─────────────────────────────────────────────────── */
  const stateNode    = document.getElementById("checkoutClientState");
  const form         = document.getElementById("premiumCheckoutForm");
  const cardFrame    = document.querySelector("[data-duffel-card-frame]");
  const statusEl     = document.querySelector("[data-duffel-card-status]");
  const cardIdInput  = document.querySelector("[data-duffel-card-id]");
  const threeDSInput = document.querySelector("[data-duffel-three-ds-id]");
  const hiddenAnc    = document.getElementById("ancillariesPayload");
  const skeleton     = document.querySelector("[data-pmt-skeleton]");
  const duffelShell  = document.querySelector("[data-pmt-duffel-shell]");
  const stepsEl      = document.querySelector("[data-pmt-steps]");

  if (!stateNode || !form || !cardFrame) return;

  /* ── Parse checkout state ─────────────────────────────────────── */
  let state = {};
  try { state = JSON.parse(stateNode.textContent || "{}"); } catch (_) {}

  const payment   = state.payment || {};
  const clientKey = String(payment.component_client_key || "").trim();
  if ((payment.mode || "").toLowerCase() !== "card" || !clientKey) return;

  /* ── Mutable state ────────────────────────────────────────────── */
  let cardMounted              = false;
  let cardValid                = false;
  let isAuthenticating         = false;
  let submitAfterAuth          = false;
  let createThreeDSecureSessionFn = null;

  /* ── Status display ───────────────────────────────────────────── */
  function setStatus(message, tone) {
    if (!statusEl) return;
    statusEl.textContent = message;
    statusEl.className = "pmt-status-bar";
    if (tone) statusEl.classList.add("is-" + tone);
  }

  /* ── Button busy state ────────────────────────────────────────── */
  function setBusy(isBusy) {
    form.querySelectorAll("[data-final-submit='true']").forEach(function (btn) {
      btn.disabled = !!isBusy;
      btn.setAttribute("aria-busy", isBusy ? "true" : "false");
    });
  }

  /* ── 3DS progress steps ───────────────────────────────────────── */
  function showSteps() {
    if (!stepsEl) return;
    stepsEl.hidden = false;

    /* Step 1 already done (card captured); step 2 active (3DS in flight) */
    stepsEl.querySelectorAll(".pmt-step").forEach(function (el) {
      el.classList.remove("is-done", "is-active");
    });
    var s1 = stepsEl.querySelector("[data-step='1']");
    var s2 = stepsEl.querySelector("[data-step='2']");
    if (s1) s1.classList.add("is-done");
    if (s2) s2.classList.add("is-active");
  }

  function advanceStep(completedNum) {
    if (!stepsEl) return;
    var done = stepsEl.querySelector("[data-step='" + completedNum + "']");
    var next = stepsEl.querySelector("[data-step='" + (completedNum + 1) + "']");
    if (done) { done.classList.remove("is-active"); done.classList.add("is-done"); }
    if (next) { next.classList.remove("is-pending"); next.classList.add("is-active"); }
  }

  function hideSteps() {
    if (stepsEl) {
      stepsEl.querySelectorAll(".pmt-step").forEach(function (el) {
        el.classList.remove("is-done", "is-active");
      });
      stepsEl.hidden = true;
    }
  }

  /* ── Ancillary services for 3DS ───────────────────────────────── */
  function parseAncillaries() {
    if (!hiddenAnc) return {};
    try {
      var p = JSON.parse(hiddenAnc.value || "{}");
      return p && typeof p === "object" ? p : {};
    } catch (_) { return {}; }
  }

  function servicesForThreeDS() {
    var payload = parseAncillaries();
    var src = Array.isArray(payload.services)
      ? payload.services
      : Array.isArray(payload.selected_services)
        ? payload.selected_services
        : [];
    return src
      .map(function (s) {
        return {
          id: String(s && s.id ? s.id : "").trim(),
          quantity: parseInt(String((s && s.quantity) || 1), 10) || 1,
        };
      })
      .filter(function (s) { return s.id; });
  }

  /* ── Poll for Duffel globals ──────────────────────────────────── */
  function waitForCardFormGlobals(attempt) {
    attempt = attempt || 0;
    var required = [
      "renderDuffelCardFormCustomElement",
      "onValidateSuccess",
      "onValidateFailure",
      "onCreateCardForTemporaryUseSuccess",
      "onCreateCardForTemporaryUseFailure",
      "createCardForTemporaryUse",
    ];
    var ready = required.every(function (fn) { return typeof window[fn] === "function"; });
    if (ready) { mountCardForm(); return; }
    if (attempt >= 80) {
      setStatus("Secure card fields could not load. Please refresh.", "error");
      return;
    }
    window.setTimeout(function () { waitForCardFormGlobals(attempt + 1); }, 100);
  }

  /* ── Lazy-load 3DS helper ─────────────────────────────────────── */
  async function loadThreeDSecureHelper() {
    if (createThreeDSecureSessionFn) return createThreeDSecureSessionFn;
    if (typeof window.createThreeDSecureSession === "function") {
      createThreeDSecureSessionFn = window.createThreeDSecureSession;
      return createThreeDSecureSessionFn;
    }
    const mod = await import(
      "https://cdn.jsdelivr.net/npm/@duffel/components@" + DUFFEL_VERSION + "/+esm"
    );
    if (typeof mod.createThreeDSecureSession !== "function") {
      throw new Error("Duffel 3DS helper is unavailable.");
    }
    createThreeDSecureSessionFn = mod.createThreeDSecureSession;
    return createThreeDSecureSessionFn;
  }

  /* ── Styles injected into the Duffel hosted iframe ───────────── */
  /* Pass only the font family so Duffel's own design language shows. */
  function cardStyles() {
    var font = "'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";
    return {
      layoutGrid:  { "font-family": font },
      cardForm:    { "font-family": font },
      input:       { default: { "font-family": font } },
      select:      { default: { "font-family": font } },
      label:       { "font-family": font },
      inputErrorMessage: { "font-family": font },
    };
  }

  /* ── Mount Duffel card form ───────────────────────────────────── */
  function mountCardForm() {
    try {
      window.renderDuffelCardFormCustomElement({
        clientKey,
        intent: "to-create-card-for-temporary-use",
        styles: cardStyles(),
      });

      window.onValidateSuccess(function () {
        cardValid = true;
        setStatus("Card ready — tap Pay to complete your booking.", "success");
      });

      window.onValidateFailure(function () {
        cardValid = false;
        setStatus("Please check the highlighted card fields.", "error");
      });

      /* Fade skeleton out, then reveal the Duffel shell */
      if (skeleton) {
        skeleton.style.transition = "opacity 200ms ease";
        skeleton.style.opacity = "0";
        window.setTimeout(function () { skeleton.hidden = true; }, 220);
      }
      if (duffelShell) {
        window.setTimeout(function () {
          duffelShell.classList.add("is-visible");
        }, 120);
      }

      cardMounted = true;
      setStatus("Enter your card details to continue.", "");
    } catch (err) {
      setStatus(err && err.message ? err.message : "Secure card fields could not load.", "error");
    }
  }

  /* ── Tokenise card via Duffel ─────────────────────────────────── */
  function createTemporaryCard() {
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = window.setTimeout(function () {
        if (settled) return;
        settled = true;
        reject(new Error("Card tokenisation timed out. Please try again."));
      }, 45000);

      window.onCreateCardForTemporaryUseSuccess(function (data) {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        resolve(data);
      });

      window.onCreateCardForTemporaryUseFailure(function (err) {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        reject(new Error((err && err.message) || "Duffel could not validate this card."));
      });

      window.createCardForTemporaryUse();
    });
  }

  /* ── Form submit intercept ────────────────────────────────────── */
  form.addEventListener(
    "submit",
    async function (event) {
      var submitter = event.submitter;
      var isFinalSubmit = !!(submitter && submitter.matches("[data-final-submit='true']"));

      /* Pass through if not the pay button, already authed, or 3DS ID already set */
      if (!isFinalSubmit || submitAfterAuth || (threeDSInput && threeDSInput.value)) return;
      if (!form.checkValidity()) return;

      event.preventDefault();
      event.stopImmediatePropagation();

      if (isAuthenticating) return;

      if (!cardMounted) {
        setStatus("Secure card fields are still loading. Please wait.", "error");
        return;
      }
      if (!cardValid) {
        setStatus("Please complete all card fields before proceeding.", "error");
        return;
      }

      isAuthenticating = true;
      setBusy(true);

      try {
        /* 1 — Tokenise */
        setStatus("Securing your card details…", "working");
        const card   = await createTemporaryCard();
        const cardId = String((card && card.id) || "").trim();
        if (!cardId) throw new Error("Duffel did not return a card identifier.");
        if (cardIdInput) cardIdInput.value = cardId;

        /* 2 — 3DS authentication */
        showSteps();
        setStatus("Authenticating with your bank…", "working");
        const create3DS = await loadThreeDSecureHelper();
        const session   = await create3DS(
          clientKey,
          cardId,
          String(state.offer_id || ""),
          servicesForThreeDS(),
          true,
        );

        if (!session || session.status !== "ready_for_payment") {
          throw new Error("Card authentication was not completed. Please try again.");
        }

        advanceStep(2);
        if (threeDSInput) threeDSInput.value = String(session.id || "");

        /* 3 — Submit */
        setStatus("Authentication complete. Finalising your booking…", "success");
        submitAfterAuth = true;
        setBusy(false);
        form.requestSubmit(submitter);
      } catch (err) {
        console.error("[Duffel] Card payment error:", err);
        hideSteps();
        setStatus(
          err && err.message ? err.message : "Card authentication failed. Please try again.",
          "error",
        );
        setBusy(false);
        isAuthenticating = false;
      }
    },
    true,
  );

  /* ── Kick off ─────────────────────────────────────────────────── */
  waitForCardFormGlobals(0);
})();
