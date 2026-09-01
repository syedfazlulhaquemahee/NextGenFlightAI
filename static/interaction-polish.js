/* Shared, dependency-free enhancement for the mobile interaction system. */
(() => {
  "use strict";

  const root = document.documentElement;
  if (!root.id) root.id = "skairova-interaction-root";
  root.dataset.skairovaInteraction = "ready";

  const interactiveSelector = [
    "a[href]",
    "button",
    "summary",
    "[role='button']",
    "[role='tab']",
    "[role='menuitem']",
    "[role='option']",
    "input[type='button']",
    "input[type='submit']",
    "input[type='reset']",
    "[onclick]"
  ].join(",");

  const focusableSelector = [
    interactiveSelector,
    "input:not([type='hidden'])",
    "textarea",
    "select",
    "[contenteditable='true']"
  ].join(",");

  const navSelector = [
    "[role='tab']",
    "[role='menuitem']",
    "[role='option']",
    ".site-nav-link",
    ".hdr-mobile-link",
    ".hdr-text-link",
    ".portal-menu-item",
    ".agent-nav-link",
    ".nu-suggest-item",
    ".lab-editor-airport-option",
    ".checkout-phone-option"
  ].join(",");

  const cardSelector = [
    "a[class*='card']",
    "button[class*='card']",
    "[role='button'][class*='card']",
    "[onclick][class*='card']",
    ".hotel-result-card a",
    ".lab-card [data-lab-details-toggle]"
  ].join(",");

  const iconSelector = [
    ".hdr-menu-btn",
    ".hdr-mobile-close",
    ".ai-chat-trigger",
    ".ai-chat-close",
    ".nu-search-btn",
    ".nu-ai-mic",
    ".lab-search-editor-close",
    ".lab-editor-swap",
    ".lab-mobile-filter-trigger",
    ".checkout-phone-trigger",
    ".checkout-phone-clear",
    ".sidebar-toggle",
    ".notif-bell"
  ].join(",");

  function isDisabled(element) {
    return element.hasAttribute("disabled") || element.getAttribute("aria-disabled") === "true";
  }

  function isIconOnly(element) {
    if (element.matches(iconSelector)) return true;
    if (!element.matches("button[aria-label], [role='button'][aria-label]")) return false;
    return (element.textContent || "").trim().length < 3;
  }

  function enhance(element) {
    if (!(element instanceof Element) || element.matches("[data-sk-interaction-skip]")) return;
    if (element.matches("input[type='hidden']")) return;

    element.classList.add("sk-focusable");
    if (element.matches(navSelector)) {
      element.classList.add("sk-nav-pressable");
    } else if (element.matches(cardSelector)) {
      element.classList.add("sk-card-pressable");
    } else if (element.matches("a[href]")) {
      element.classList.add("sk-text-pressable");
    } else {
      element.classList.add("sk-pressable");
    }

    if (isIconOnly(element)) element.classList.add("sk-icon-pressable");
  }

  function enhanceFocus(element) {
    if (!(element instanceof Element) || element.matches("[data-sk-interaction-skip], input[type='hidden']")) return;
    element.classList.add("sk-focusable");
  }

  function enhanceTree(scope) {
    if (!(scope instanceof Element || scope instanceof Document)) return;
    if (scope instanceof Element && scope.matches(interactiveSelector)) enhance(scope);
    if (scope instanceof Element && scope.matches(focusableSelector)) enhanceFocus(scope);
    scope.querySelectorAll?.(interactiveSelector).forEach(enhance);
    scope.querySelectorAll?.(focusableSelector).forEach(enhanceFocus);
  }

  let activePress = null;
  function releasePress() {
    if (!activePress) return;
    activePress.classList.remove("is-pressed");
    activePress = null;
  }

  document.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
    const target = event.target instanceof Element
      ? event.target.closest(".sk-pressable, .sk-icon-pressable, .sk-nav-pressable, .sk-card-pressable, .sk-text-pressable")
      : null;
    if (!target || isDisabled(target)) return;
    releasePress();
    activePress = target;
    target.classList.add("is-pressed");
  }, { passive: true });

  document.addEventListener("pointerup", releasePress, { passive: true });
  document.addEventListener("pointercancel", releasePress, { passive: true });
  document.addEventListener("dragstart", releasePress, { passive: true });
  window.addEventListener("blur", releasePress, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) releasePress();
  });

  /* A reference-counted helper for sheets and dialogs. Existing overlay
   * classes remain respected, so this can be adopted incrementally. */
  const scrollLocks = new Set();
  window.SkairovaInteraction = window.SkairovaInteraction || {};
  window.SkairovaInteraction.lockScroll = (key = "default") => {
    scrollLocks.add(key);
    root.classList.add("sk-scroll-locked");
  };
  window.SkairovaInteraction.unlockScroll = (key = "default") => {
    scrollLocks.delete(key);
    if (!scrollLocks.size) root.classList.remove("sk-scroll-locked");
  };

  const start = () => {
    enhanceTree(document);
    const observer = new MutationObserver((records) => {
      records.forEach((record) => {
        record.addedNodes.forEach((node) => {
          if (node.nodeType === Node.ELEMENT_NODE) enhanceTree(node);
        });
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
