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

  /* ── Mobile drawer — full-screen takeover, no backdrop needed ── */
  var btn = document.getElementById("hdrMenuBtn");
  var panel = document.getElementById("hdrMobilePanel");
  if (!btn || !panel) return;

  function isOpen() {
    return !panel.hidden;
  }

  function open() {
    panel.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    document.documentElement.classList.add("hdr-menu-open");
  }

  function close() {
    panel.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    document.documentElement.classList.remove("hdr-menu-open");
  }

  btn.addEventListener("click", function () {
    if (isOpen()) close();
    else open();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen()) close();
  });

  // Any tap inside the panel — a nav link, an auth trigger, the close
  // button, the sign-out button — closes it. Links still navigate
  // normally; data-auth-open triggers still open the sign-in dialog
  // (auth-block.js listens globally).
  panel.addEventListener("click", function (event) {
    if (event.target.closest("a, button")) close();
  });

  // Crossing back above the 920px breakpoint (resize, rotation) should not
  // leave the panel stuck open underneath a now-visible desktop nav.
  var desktopQuery = window.matchMedia("(min-width: 921px)");
  desktopQuery.addEventListener("change", function (event) {
    if (event.matches) close();
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
