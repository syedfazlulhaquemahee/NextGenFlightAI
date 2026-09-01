/* Header behavior: the traveling nav underline indicator, and the mobile
   hamburger drawer. Modeled on React Bits Pro's "Navigation 15". */
(function () {
  "use strict";

  /* ── Traveling underline indicator ──
     One shared absolutely-positioned bar, not a pseudo-element per link:
     on hover/focus it animates its transform/width to match whichever
     link triggered it, so it visibly slides across the gap between items.
     It rests under the active page's link when nothing is hovered, and
     hides entirely if no link is active (e.g. on a page outside the nav's
     three sections). */
  (function initNavIndicator() {
    var nav = document.getElementById("siteNav");
    var indicator = document.getElementById("siteNavIndicator");
    if (!nav || !indicator) return;

    var links = Array.prototype.slice.call(nav.querySelectorAll(".site-nav-link"));
    if (!links.length) return;

    function place(el) {
      var navRect = nav.getBoundingClientRect();
      var rect = el.getBoundingClientRect();
      indicator.style.transform = "translateX(" + (rect.left - navRect.left) + "px)";
      indicator.style.width = rect.width + "px";
      indicator.style.opacity = "1";
    }

    function restToActive() {
      var active = nav.querySelector(".site-nav-link.site-nav-active");
      if (active) place(active);
      else indicator.style.opacity = "0";
    }

    links.forEach(function (link) {
      link.addEventListener("mouseenter", function () { place(link); });
      link.addEventListener("focus", function () { place(link); });
    });

    nav.addEventListener("mouseleave", restToActive);
    nav.addEventListener("focusout", function (event) {
      if (!nav.contains(event.relatedTarget)) restToActive();
    });

    // The home page's own hero tab switcher (index.html) also toggles
    // site-nav-active on these same links — e.g. clicking "Hotels" in the
    // hero, not just in this nav — without any page reload. Without this
    // observer the indicator never learns that happened and stays parked
    // under whichever link was active at page load, silently disagreeing
    // with both the hero and the link's own (correctly updated) text
    // color/weight.
    var classObserver = new MutationObserver(function () {
      if (!nav.matches(":hover") && !nav.contains(document.activeElement)) restToActive();
    });
    links.forEach(function (link) {
      classObserver.observe(link, { attributes: true, attributeFilter: ["class"] });
    });

    // Fonts loading late can shift link widths after the initial measure.
    restToActive();
    window.addEventListener("resize", restToActive);
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(restToActive);
    }
  })();

  /* ── Mobile drawer — full-screen takeover, no backdrop needed ──
     Keep the native-feeling animation in CSS and the interaction state here:
     a modal dialog needs focus management and a scroll lock that preserves
     the page's exact position when the menu closes. */
  var btn = document.getElementById("hdrMenuBtn");
  var panel = document.getElementById("hdrMobilePanel");
  if (!btn || !panel) return;

  var closeBtn = document.getElementById("hdrMobileClose");
  var desktopQuery = window.matchMedia("(min-width: 921px)");
  var reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  var drawerState = panel.hidden ? "closed" : "open";
  var scrollPosition = 0;
  var lastFocusedElement = null;
  var restoreFocusOnClose = false;
  var openFrame = 0;
  var openTimer = 0;
  var closeTimer = 0;

  function isOpen() {
    return drawerState === "opening" || drawerState === "open";
  }

  function setPanelInert(inert) {
    if (inert) panel.setAttribute("inert", "");
    else panel.removeAttribute("inert");
  }

  function safeFocus(element) {
    if (!element || !document.documentElement.contains(element)) return;
    try {
      element.focus({ preventScroll: true });
    } catch (error) {
      element.focus();
    }
  }

  function restoreMenuTriggerFocus() {
    // Safari does not always focus a button after a pointer tap, so the
    // recorded opener can be <body>. In that case, use the known trigger
    // rather than leaving keyboard users at the start of the document.
    var target = lastFocusedElement && lastFocusedElement !== document.body && lastFocusedElement !== document.documentElement
      ? lastFocusedElement
      : btn;
    safeFocus(target);
  }

  function lockPageScroll() {
    scrollPosition = window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0;
    document.documentElement.style.setProperty("--hdr-menu-scroll-offset", (-scrollPosition) + "px");
    document.documentElement.classList.add("hdr-menu-open");
    document.body.classList.add("hdr-menu-open");
  }

  function unlockPageScroll() {
    document.documentElement.classList.remove("hdr-menu-open");
    document.body.classList.remove("hdr-menu-open");
    document.documentElement.style.removeProperty("--hdr-menu-scroll-offset");
    window.scrollTo(0, scrollPosition);
  }

  function focusableItems() {
    var selector = [
      "a[href]",
      "button:not([disabled])",
      "input:not([disabled]):not([type='hidden'])",
      "select:not([disabled])",
      "textarea:not([disabled])",
      "[tabindex]:not([tabindex='-1'])",
    ].join(",");

    return Array.prototype.slice.call(panel.querySelectorAll(selector)).filter(function (element) {
      return element.getClientRects().length > 0 && !element.hasAttribute("inert");
    });
  }

  function finishClose() {
    if (drawerState !== "closing") return;

    window.clearTimeout(closeTimer);
    panel.hidden = true;
    panel.classList.remove("is-open", "is-closing");
    panel.setAttribute("aria-hidden", "true");
    setPanelInert(true);
    unlockPageScroll();
    drawerState = "closed";

    if (restoreFocusOnClose) {
      restoreMenuTriggerFocus();
    }
    restoreFocusOnClose = false;
  }

  function finishOpen() {
    if (drawerState !== "opening") return;

    if (openFrame) window.cancelAnimationFrame(openFrame);
    window.clearTimeout(openTimer);
    openFrame = 0;
    openTimer = 0;
    panel.classList.add("is-open");
    drawerState = "open";
    safeFocus(closeBtn || panel);
  }

  function open() {
    if (drawerState === "open" || drawerState === "opening") return;

    window.clearTimeout(closeTimer);
    if (openFrame) window.cancelAnimationFrame(openFrame);
    window.clearTimeout(openTimer);

    lastFocusedElement = document.activeElement;
    restoreFocusOnClose = false;
    panel.hidden = false;
    panel.classList.remove("is-closing");
    panel.setAttribute("aria-hidden", "false");
    setPanelInert(false);
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-label", "Close menu");
    lockPageScroll();
    drawerState = "opening";

    // Waiting a frame lets the browser paint the closed state before the
    // .is-open transition is applied. The short timer fallback covers a
    // backgrounded browser that temporarily pauses requestAnimationFrame.
    openFrame = window.requestAnimationFrame(finishOpen);
    openTimer = window.setTimeout(finishOpen, 48);
  }

  function close(options) {
    options = options || {};
    if (drawerState === "closed") return;
    if (drawerState === "closing") {
      if (options.immediate) finishClose();
      return;
    }

    if (openFrame) {
      window.cancelAnimationFrame(openFrame);
      openFrame = 0;
    }
    window.clearTimeout(openTimer);
    openTimer = 0;

    restoreFocusOnClose = options.restoreFocus === true;
    drawerState = "closing";
    panel.classList.remove("is-open");
    panel.classList.add("is-closing");
    panel.setAttribute("aria-hidden", "true");
    setPanelInert(true);
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-label", "Open menu");

    if (restoreFocusOnClose) restoreMenuTriggerFocus();

    if (options.immediate || reducedMotionQuery.matches) {
      finishClose();
      return;
    }

    // opacity is the reliably-transitioned property across the supported
    // browsers; the timeout prevents a stale drawer if a transition is
    // interrupted by an orientation change or a page lifecycle event.
    closeTimer = window.setTimeout(finishClose, 300);
  }

  btn.addEventListener("click", function () {
    if (isOpen()) close();
    else open();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen()) {
      event.preventDefault();
      close({ restoreFocus: true });
      return;
    }

    if (event.key !== "Tab" || !isOpen()) return;

    var items = focusableItems();
    if (!items.length) {
      event.preventDefault();
      safeFocus(panel);
      return;
    }

    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {
      event.preventDefault();
      safeFocus(last);
    } else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
      event.preventDefault();
      safeFocus(first);
    }
  });

  panel.addEventListener("transitionend", function (event) {
    if (event.target === panel && event.propertyName === "opacity") finishClose();
  });

  // Any link/action in the panel should dismiss it. Navigation continues
  // normally; data-auth-open still reaches its global handler after this
  // one has started the drawer's exit transition.
  panel.addEventListener("click", function (event) {
    var control = event.target.closest("a, button");
    if (!control || !panel.contains(control)) return;
    close({ restoreFocus: control === closeBtn });
  });

  // Crossing back above the 920px breakpoint (resize, rotation) should not
  // leave the panel stuck open underneath a now-visible desktop nav.
  var onDesktopChange = function (event) {
    if (event.matches) close({ immediate: true });
  };
  if (desktopQuery.addEventListener) desktopQuery.addEventListener("change", onDesktopChange);
  else desktopQuery.addListener(onDesktopChange);

  // A bfcache restore should never resurrect a visually closed but scroll-
  // locking drawer from the previous page visit.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted && (isOpen() || drawerState === "closing")) {
      close({ immediate: true });
    }
  });

  /* Header message icon — a second, always-visible entry point into the
     existing floating AI chat (static/ai-assistant.js). Simplest correct
     wiring: forward to the real trigger button rather than duplicating its
     open/close/badge/quick-reply logic here. */
  var headerChatBtn = document.getElementById("hdrOpenChatBtn");
  var headerChatDot = document.getElementById("hdrOpenChatDot");
  if (headerChatBtn) {
    headerChatBtn.addEventListener("click", function () {
      var realTrigger = document.querySelector("#aiChatBubbleWrap .ai-chat-trigger");
      if (realTrigger) realTrigger.click();
    });
  }

  /* The dot mirrors the real .ai-chat-badge the floating widget already
     shows/hides itself — never set independently, so it can't drift into
     a permanent fake "you have a notification" state. That badge is
     removed from the DOM (not just hidden) once the chat is opened, so
     childList must be watched too, not just the style attribute. */
  if (headerChatDot) {
    var wrap = document.getElementById("aiChatBubbleWrap");
    var syncDot = function () {
      var badge = wrap && wrap.querySelector(".ai-chat-badge");
      var visible = !!badge && badge.style.display !== "none";
      headerChatDot.hidden = !visible;
    };
    syncDot();
    if (wrap && window.MutationObserver) {
      new MutationObserver(syncDot).observe(wrap, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["style"],
      });
    }
  }
})();
