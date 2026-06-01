(function () {
  const warning = "Going back may cause this live price to change or become unavailable. Leave this page?";
  const backLinks = Array.from(document.querySelectorAll("[data-review-back='true']"));
  const root = document.documentElement;

  const syncStickySummaryOffset = () => {
    const header = document.querySelector(".topbar.platform-header") || document.querySelector(".topbar");
    const layout = document.querySelector(".review-layout--new") || document.querySelector(".review-layout");
    const main = document.querySelector(".review-main");
    const summary = document.querySelector(".review-summary-panel");
    const headerHeight = header ? Math.ceil(header.getBoundingClientRect().height) : 0;
    const viewportHeight = Math.ceil(window.visualViewport?.height || window.innerHeight || root.clientHeight || 0);
    const stickyGap = window.matchMedia("(max-width: 1080px)").matches ? 0 : 18;
    const viewportPad = 18;
    const availableTop = headerHeight + viewportPad;
    const availableBottom = Math.max(availableTop, viewportHeight - viewportPad);
    const summaryHeight = summary ? Math.ceil(summary.getBoundingClientRect().height) : 0;
    const idealCenter = headerHeight + Math.max(0, viewportHeight - headerHeight) / 2;
    let centeredTop = idealCenter;
    let stickerScale = 1;

    if (summaryHeight > 0) {
      const availableHeight = Math.max(1, availableBottom - availableTop);
      if (summaryHeight > availableHeight) {
        stickerScale = Math.max(0.78, availableHeight / summaryHeight);
        centeredTop = availableTop + availableHeight / 2;
      } else {
        const halfHeight = summaryHeight / 2;
        const minCenter = availableTop + halfHeight;
        const maxCenter = availableBottom - halfHeight;
        centeredTop = Math.min(Math.max(idealCenter, minCenter), maxCenter);
      }
    }

    root.style.setProperty("--review-sticky-top", `${headerHeight + stickyGap}px`);
    root.style.setProperty("--review-sticker-center-top", `${Math.round(centeredTop)}px`);
    root.style.setProperty("--review-sticker-scale", stickerScale.toFixed(3));

    if (layout && main) {
      const layoutRect = layout.getBoundingClientRect();
      const mainRect = main.getBoundingClientRect();
      const styles = window.getComputedStyle(layout);
      const gap = Number.parseFloat(styles.columnGap || styles.gap || "0") || 0;
      const left = mainRect.right + gap;
      const width = Math.max(270, layoutRect.right - left);
      root.style.setProperty("--review-sticker-left", `${Math.round(left)}px`);
      root.style.setProperty("--review-sticker-width", `${Math.round(width)}px`);
    }
  };

  let stickyOffsetFrame = 0;
  const scheduleStickySummaryOffset = () => {
    if (stickyOffsetFrame) return;
    stickyOffsetFrame = window.requestAnimationFrame(() => {
      stickyOffsetFrame = 0;
      syncStickySummaryOffset();
    });
  };

  syncStickySummaryOffset();
  window.addEventListener("resize", scheduleStickySummaryOffset, { passive: true });
  window.addEventListener("orientationchange", scheduleStickySummaryOffset, { passive: true });
  window.visualViewport?.addEventListener("resize", scheduleStickySummaryOffset, { passive: true });

  const initMobileCheckoutBar = () => {
    const mobileBar = document.querySelector(".review-mobile-checkout-bar");
    const summaryTotal = document.querySelector(".review-summary-total");
    const summaryCta = document.querySelector(".review-summary-cta");
    if (!mobileBar || (!summaryTotal && !summaryCta)) return;

    const isMobile = () => window.matchMedia("(max-width: 520px)").matches;
    let totalVisible = false;
    let ctaVisible = false;

    const setVisible = (visible) => {
      if (!isMobile()) {
        mobileBar.hidden = true;
        mobileBar.classList.remove("is-visible");
        return;
      }
      mobileBar.hidden = !visible;
      mobileBar.classList.toggle("is-visible", visible);
    };

    const isElementVisible = (el) => {
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const viewportHeight = window.visualViewport?.height || window.innerHeight || root.clientHeight || 0;
      return rect.bottom > 0 && rect.top < viewportHeight;
    };

    const sync = () => {
      if (summaryTotal) totalVisible = isElementVisible(summaryTotal);
      if (summaryCta) ctaVisible = isElementVisible(summaryCta);
      setVisible(!totalVisible && !ctaVisible);
    };

    if ("IntersectionObserver" in window) {
      const observer = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.target === summaryTotal) totalVisible = entry.isIntersecting;
            if (entry.target === summaryCta) ctaVisible = entry.isIntersecting;
          });
          setVisible(!totalVisible && !ctaVisible);
        },
        { threshold: 0.01 },
      );
      if (summaryTotal) observer.observe(summaryTotal);
      if (summaryCta) observer.observe(summaryCta);
    }

    sync();
    window.addEventListener("scroll", sync, { passive: true });
    window.addEventListener("resize", sync, { passive: true });
    window.visualViewport?.addEventListener("resize", sync, { passive: true });
  };

  initMobileCheckoutBar();

  if ("ResizeObserver" in window) {
    const header = document.querySelector(".topbar.platform-header") || document.querySelector(".topbar");
    if (header) {
      new ResizeObserver(scheduleStickySummaryOffset).observe(header);
    }
  }

  backLinks.forEach((link) => {
    link.addEventListener("click", (event) => {
      if (window.confirm(warning)) return;
      event.preventDefault();
    });
  });

  if (window.history && window.history.pushState) {
    window.history.pushState({ reviewGuard: true }, "", window.location.href);
    window.addEventListener("popstate", () => {
      if (window.confirm(warning)) {
        window.history.go(-1);
      } else {
        window.history.pushState({ reviewGuard: true }, "", window.location.href);
      }
    });
  }

  const reviewDetails = Array.from(document.querySelectorAll(".review-details"));
  reviewDetails.forEach((details) => {
    const toggle = details.querySelector(".flight-details-toggle");
    if (!toggle) return;

    const syncState = () => {
      toggle.setAttribute("aria-expanded", details.open ? "true" : "false");
    };

    syncState();
    details.addEventListener("toggle", syncState);
  });

  const fareHost = document.getElementById("fare-options-host");
  if (fareHost) {
    const fareUrl = (fareHost.getAttribute("data-fare-options-url") || "").trim();
    if (fareUrl) {
      fetch(fareUrl)
        .then((res) => (res.ok ? res.text() : null))
        .then((html) => {
          if (!html || !html.trim()) {
            return;
          }
          fareHost.innerHTML = html;
          Array.from(fareHost.querySelectorAll("[data-fare-url]")).forEach((card) => {
            const url = (card.getAttribute("data-fare-url") || "").trim();
            if (!url) return;
            const go = () => window.location.assign(url);
            card.addEventListener("click", (event) => {
              const target = event.target;
              if (!(target instanceof Element)) return;
              const nested = target.closest("a, button, input, select, textarea, summary");
              if (nested && nested !== card) return;
              go();
            });
            card.addEventListener("keydown", (event) => {
              if (event.key !== "Enter" && event.key !== " ") return;
              event.preventDefault();
              go();
            });
          });
          scheduleStickySummaryOffset();
        })
        .catch(() => {});
    }
  }

  const fareCards = Array.from(document.querySelectorAll("[data-fare-url]"));
  fareCards.forEach((card) => {
    const fareUrl = (card.getAttribute("data-fare-url") || "").trim();
    if (!fareUrl) return;

    const goToFare = () => {
      window.location.assign(fareUrl);
    };

    card.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const nestedInteractive = target.closest("a, button, input, select, textarea, summary");
      if (nestedInteractive && nestedInteractive !== card) return;
      goToFare();
    });

    card.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      goToFare();
    });
  });
})();
