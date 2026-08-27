(() => {
  "use strict";
  const ACTIVE_KEY = "skairova_feedback_active_ms_v2";
  const DISMISSED_KEY = "skairova_feedback_dismissed_until_v2";
  const SUBMITTED_KEY = "skairova_feedback_submitted_v2";
  const WAIT_MS = 50_000;
  const SNOOZE_MS = 14 * 24 * 60 * 60 * 1_000;
  const get = (key, fallback = "") => { try { return localStorage.getItem(key) || fallback; } catch { return fallback; } };
  const set = (key, value) => { try { localStorage.setItem(key, String(value)); } catch { /* Storage can be unavailable. */ } };

  document.addEventListener("DOMContentLoaded", () => {
    const root = document.getElementById("skFeedback");
    if (!root || get(SUBMITTED_KEY) || Number(get(DISMISSED_KEY, "0")) > Date.now()) return;
    const card = root.querySelector(".sk-feedback-widget");
    const scoreButtons = [...root.querySelectorAll("[data-feedback-rating]")];
    const typeButtons = [...root.querySelectorAll("[data-feedback-type]")];
    const submit = root.querySelector("[data-feedback-submit]");
    const message = root.querySelector("#skFeedbackComment");
    const scoreSelection = root.querySelector("#skFeedbackScoreSelection");
    const formStep = root.querySelector('[data-feedback-step="form"]');
    const sentStep = root.querySelector('[data-feedback-step="sent"]');
    let startedAt = document.visibilityState === "visible" ? Date.now() : 0;
    let timer, opened = false, rating = null, feedbackType = "idea", previousFocus = null;
    const elapsed = () => Math.max(0, Number(get(ACTIVE_KEY, "0")) || 0);
    const saveTime = () => {
      if (!startedAt) return elapsed();
      const total = elapsed() + Math.max(0, Date.now() - startedAt);
      set(ACTIVE_KEY, total);
      startedAt = document.visibilityState === "visible" ? Date.now() : 0;
      return total;
    };
    const eligible = () => !get(SUBMITTED_KEY) && Number(get(DISMISSED_KEY, "0")) <= Date.now();
    const show = () => {
      if (opened || !eligible()) return;
      opened = true; previousFocus = document.activeElement; root.hidden = false;
      root.setAttribute("aria-hidden", "false"); document.documentElement.classList.add("sk-feedback-open");
      requestAnimationFrame(() => root.classList.add("is-visible")); card.focus({ preventScroll: true });
    };
    const close = (snooze = true) => {
      if (!opened) return;
      opened = false;
      if (snooze && !get(SUBMITTED_KEY)) set(DISMISSED_KEY, Date.now() + SNOOZE_MS);
      root.classList.remove("is-visible"); root.setAttribute("aria-hidden", "true"); document.documentElement.classList.remove("sk-feedback-open");
      setTimeout(() => { if (!opened) root.hidden = true; }, 170); previousFocus?.focus?.({ preventScroll: true });
    };
    const maybeShow = () => { if (eligible() && saveTime() >= WAIT_MS) { clearInterval(timer); show(); } };
    const updateSubmit = () => { submit.disabled = rating === null || !message.value.trim(); };
    typeButtons.forEach((button) => button.addEventListener("click", () => {
      feedbackType = button.dataset.feedbackType;
      typeButtons.forEach((item) => { const active = item === button; item.classList.toggle("is-selected", active); item.setAttribute("aria-checked", String(active)); });
      message.placeholder = feedbackType === "issue" ? "What happened, and what did you expect instead?" : feedbackType === "praise" ? "What worked especially well?" : "What would you like Skairova to do better?";
    }));
    scoreButtons.forEach((button) => button.addEventListener("click", () => {
      rating = Number(button.dataset.feedbackRating);
      scoreButtons.forEach((item) => { const active = item === button; item.classList.toggle("is-selected", active); item.setAttribute("aria-checked", String(active)); });
      scoreSelection.textContent = `${rating} out of 10 selected`;
      updateSubmit();
    }));
    message.addEventListener("input", updateSubmit);
    submit.addEventListener("click", async () => {
      if (submit.disabled) return;
      submit.disabled = true; submit.textContent = "Sending…";
      try {
        const response = await fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify({ rating, comment: message.value.trim(), page: `${location.pathname}${location.search}`, feedback_type: feedbackType }) });
        if (!response.ok) throw new Error("Request failed");
        set(SUBMITTED_KEY, "1"); formStep.hidden = true; sentStep.hidden = false; sentStep.querySelector("button").focus({ preventScroll: true });
      } catch { submit.disabled = false; submit.textContent = "Try again"; }
    });
    root.querySelectorAll("[data-feedback-dismiss]").forEach((button) => button.addEventListener("click", () => close()));
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { startedAt = Date.now(); maybeShow(); } else saveTime(); });
    window.addEventListener("pagehide", saveTime);
    document.addEventListener("keydown", (event) => { if (opened && event.key === "Escape") close(); });
    timer = setInterval(maybeShow, 1_000); maybeShow();
  });
})();
