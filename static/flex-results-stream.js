(function () {
  const flexForm = document.getElementById("flexStreamBootForm");
  const pendingForm = document.getElementById("resultsPendingBootForm");
  const bootForm = flexForm || pendingForm;
  if (!bootForm) return;

  const isStandardSearchStream = Boolean(pendingForm && !flexForm);
  const streamUrl = new URL(
    isStandardSearchStream ? "/search/stream" : "/search/flex-stream",
    window.location.origin,
  ).toString();

  const statusEl = document.getElementById("resultsFlexLiveStatus");
  const statusDetailEl = document.getElementById("resultsFlexLiveDetail");
  const chipsEl = document.getElementById("resultsFlexDateChips");
  const headDatesEl = document.getElementById("resultsFlexHeadDates");
  // #resultsCards was the classic skeleton's card container — classic markup
  // (and its whole progressive "replace shimmer slots with classic-shaped
  // live preview cards" enhancement, built entirely out of classic .flight-
  // card/fc-row HTML via buildResultsFlightCardHtml() below) is gone now, so
  // this is always null. Every downstream use is guarded rather than a hard
  // return here, because the status text / progress bar / final
  // swapInFinalResults() below don't depend on it at all and must keep
  // working regardless.
  const cardsEl = document.getElementById("resultsCards");

  // Always true now — there is no classic markup left to populate. Kept as
  // a named constant (rather than deleting the `if (!classicHidden)` guard
  // below) so the intent stays obvious if a classic-shaped preview card
  // ever comes back.
  const classicHidden = true;

  const progressFill = document.getElementById("resultsFlexProgressFill");

  const skeletonSlots = cardsEl ? Array.from(cardsEl.querySelectorAll(".flex-live-skeleton")) : [];
  let standardFlightTotal = 0;
  const maxChips = 18;
  const provisionalRows = new Map();
  let provisionalWrap = null;

  let loadProgress = 0.04;
  let datesPlanned1 = 0;
  let datesPlanned2 = 0;
  let phase1Scans = 0;
  let phase2Scans = 0;
  let routeOrigin = "";
  let routeDest = "";
  let tripOneway = false;
  let bestScanPrice = Infinity;

  function setStatusDetail(text) {
    if (statusDetailEl) statusDetailEl.textContent = text == null ? "" : String(text);
  }

  function bumpProgress(target) {
    const t = Math.min(0.98, Math.max(loadProgress, Number(target) || 0));
    loadProgress = t;
    if (progressFill) progressFill.style.width = (t * 100).toFixed(2) + "%";
  }

  function routeStr() {
    const a = String(routeOrigin || "").trim().toUpperCase();
    const b = String(routeDest || "").trim().toUpperCase();
    if (a && b) return a + "–" + b;
    return "";
  }

  function formatHitPrimary(hit) {
    const h = hit || {};
    const dep = h.depart_date ? String(h.depart_date) : "";
    const ret = h.return_date ? String(h.return_date) : "";
    if (tripOneway) {
      return dep ? "Outbound quote · " + dep : "Outbound date check";
    }
    if (dep && ret) return "Round-trip quote · " + dep + " → " + ret;
    return dep ? "Quote · " + dep : "Calendar quote";
  }

  function scanProgressFrac(phase, done, planned) {
    const p = Math.max(1, Number(planned) || 1);
    const d = Math.max(0, Number(done) || 0);
    const ratio = Math.min(1, d / p);
    if (phase === 2) {
      return 0.64 + 0.18 * ratio;
    }
    return 0.12 + 0.5 * ratio;
  }

  function escapeHtml(str) {
    const d = document.createElement("div");
    d.textContent = str == null ? "" : String(str);
    return d.innerHTML;
  }

  function formatMoney(currency, amount) {
    const n = Number(amount);
    if (!Number.isFinite(n)) return "";
    const cur = (currency || "USD").toUpperCase();
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: 0 }).format(n);
    } catch (e) {
      return cur + " " + n.toFixed(0);
    }
  }

  function formatScanLine(hit, tripOneway) {
    const dep = hit.depart_date || "";
    const price = formatMoney(hit.scan_currency, hit.scan_price_total);
    if (tripOneway) return dep + " · " + price;
    const ret = hit.return_date || "";
    return dep + " → " + ret + " · " + price;
  }

  function shortDate(iso) {
    if (!iso) return "";
    const p = String(iso).split("-");
    if (p.length < 3) return iso;
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return (months[parseInt(p[1], 10) - 1] || p[1]) + " " + parseInt(p[2], 10);
  }

  function updateBestPill(hit) {
    const price = Number((hit || {}).scan_price_total);
    if (!Number.isFinite(price) || price >= bestScanPrice) return;
    bestScanPrice = price;
    const el = document.getElementById("resultsFlexBestFound");
    if (!el) return;
    const dep = (hit || {}).depart_date || "";
    const ret = (hit || {}).return_date || "";
    const priceStr = formatMoney((hit || {}).scan_currency, price);
    const dateStr = tripOneway ? shortDate(dep) : (shortDate(dep) + "–" + shortDate(ret));
    el.textContent = priceStr + (dateStr ? "  ·  " + dateStr : "");
    el.removeAttribute("hidden");
  }

  function injectResultsScrollFix(html) {
    const snippet =
      "<script>(function(){try{if('scrollRestoration'in history)history.scrollRestoration='manual';}catch(e){}window.scrollTo(0,0);document.documentElement&&(document.documentElement.scrollTop=0);document.body&&(document.body.scrollTop=0);})()<\/script>";
    const lower = html.toLowerCase();
    const idx = lower.indexOf("<head");
    if (idx === -1) return snippet + html;
    const gt = html.indexOf(">", idx);
    if (gt === -1) return snippet + html;
    return html.slice(0, gt + 1) + snippet + html.slice(gt + 1);
  }

  // Scripts inserted via innerHTML are inert — browsers never execute
  // <script> elements created that way. Walk the just-swapped-in subtree
  // and recreate each one so it actually runs. Any script whose src already
  // exists OUTSIDE this subtree is skipped instead of recreated — that's
  // what stops header-menu.js/theme-toggle.js/portal-menu.js (all loaded
  // once already, outside #resultsContentRoot, before this swap ever ran)
  // from double-binding their listeners.
  //
  // Deliberately checks only scripts OUTSIDE root, not all of
  // document.scripts: by the time this runs, root.innerHTML was just
  // assigned, so the (still-inert) copies of results-lab.js/ai-assistant.js/
  // etc. we're about to replace are ALREADY in document.scripts too —
  // comparing against the unfiltered list made every script match itself
  // and get silently removed instead of executed, so nothing in Lab mode
  // ever rendered a single card.
  //
  // Returns a promise that resolves once results-lab.js has loaded (or
  // failed to), so the caller can wait for it before revealing the page.
  function executeInjectedScripts(root) {
    const originals = Array.from(root.querySelectorAll("script"));
    const outsideSrcs = new Set(
      Array.from(document.scripts)
        .filter(function (s) { return !root.contains(s); })
        .map(function (s) { return s.src; })
        .filter(Boolean)
    );
    let labLoadPromise = null;
    originals.forEach(function (old) {
      const src = old.getAttribute("src");
      if (src) {
        const absoluteSrc = new URL(src, window.location.href).href;
        if (outsideSrcs.has(absoluteSrc)) {
          old.remove();
          return;
        }
      }
      const fresh = document.createElement("script");
      Array.from(old.attributes).forEach(function (attr) {
        fresh.setAttribute(attr.name, attr.value);
      });
      // Dynamically-inserted scripts default to async; forcing this off
      // makes multiple newly-created src scripts execute in document
      // order, matching the guarantees the original deferred tags had.
      fresh.async = false;
      if (!src) fresh.textContent = old.textContent;
      const isLabScript = Boolean(src) && /(^|\/)results-lab\.js(\?|$)/.test(src);
      if (isLabScript) {
        labLoadPromise = new Promise(function (resolve) {
          fresh.addEventListener("load", resolve, { once: true });
          fresh.addEventListener("error", resolve, { once: true });
        });
      }
      old.replaceWith(fresh);
    });
    return labLoadPromise || Promise.resolve();
  }

  function nextPaint() {
    return new Promise(function (resolve) {
      requestAnimationFrame(function () {
        requestAnimationFrame(resolve);
      });
    });
  }

  function delay(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  // Non-destructive replacement for document.write(): swaps the freshly
  // completed results.html render into the live page in place, instead of
  // tearing the whole document down and rebuilding it. Same DOMParser +
  // targeted-innerHTML pattern already proven working in
  // static/liteapi-supplement.js, applied to the shell->results handoff.
  async function swapInFinalResults(html, reloadUrl) {
    const currentRoot = document.getElementById("resultsContentRoot");
    let newRoot = null;
    if (currentRoot) {
      try {
        const parsedDoc = new DOMParser().parseFromString(html, "text/html");
        newRoot = parsedDoc.getElementById("resultsContentRoot");
      } catch (e) {
        newRoot = null;
      }
    }

    if (!currentRoot || !newRoot) {
      // Defensive fallback — template drift or an unexpected error-page
      // shape. Degrade to the previously-shipped, known-working mechanism
      // rather than leaving the page stuck on the loading skeleton.
      html = injectResultsScrollFix(html);
      try {
        if ("scrollRestoration" in history) history.scrollRestoration = "manual";
      } catch (e) {
        /* ignore */
      }
      window.history.replaceState({ searchLoader: true }, "", reloadUrl);
      window.scrollTo(0, 0);
      document.open();
      document.write(html);
      document.close();
      return;
    }

    const newTitle = newRoot.ownerDocument && newRoot.ownerDocument.title;

    // Content changes instantly underneath while invisible, then fades in
    // once results-lab.js is ready — the user never sees an in-between
    // blank/broken state, and (since nothing is torn down) the browser
    // preserves scroll position through the swap with no extra handling.
    currentRoot.style.transition = "opacity .22s ease";
    currentRoot.style.opacity = "0";
    await nextPaint();

    currentRoot.innerHTML = newRoot.innerHTML;
    const labReady = executeInjectedScripts(currentRoot);

    window.history.replaceState({ searchLoader: true }, "", reloadUrl);
    if (newTitle) document.title = newTitle;

    await Promise.race([labReady, delay(800)]);

    currentRoot.style.opacity = "";
    currentRoot.style.transition = "";
  }

  function formatMoneyExact(currency, amount) {
    const n = Number(amount);
    if (!Number.isFinite(n)) return "";
    const cur = (currency || "USD").toUpperCase();
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: cur,
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(n);
    } catch (e) {
      return cur + " " + n.toFixed(2);
    }
  }

  function buildLayoverTooltip(lay) {
    const code = String(lay.code || "");
    const name = String(lay.name || code);
    const dur = String(lay.duration_label || "");
    const qual = String(lay.quality_label || "");
    const normalizedName = name.toUpperCase();
    const upperCode = code.toUpperCase();
    const head = upperCode && !normalizedName.includes("(" + upperCode + ")")
      ? name + " (" + code + ")"
      : name;
    const parts = [head];
    if (dur) parts.push("Layover: " + dur);
    if (qual) parts.push(qual);
    return escapeHtml(parts.join(" — "));
  }

  function buildLayoverSummaryLabel(layovers) {
    const labels = [];
    for (let i = 0; i < layovers.length; i++) {
      const lay = layovers[i] || {};
      const code = String(lay.code || "Stop").trim().toUpperCase();
      const duration = String(lay.duration_label || "").trim();
      if (duration && duration.toLowerCase() !== "details unavailable") {
        labels.push(code + " " + duration);
      } else {
        labels.push(code + " duration unavailable");
      }
    }
    return labels.join(" · ");
  }

  function buildFcLayoverSummaryHtml(layovers) {
    if (!layovers.length) return "";
    const summary = buildLayoverSummaryLabel(layovers);
    const prefix = layovers.length === 1 ? "Layover " : "Layovers ";
    return (
      '<span class="fc-via-layover" title="' +
      escapeHtml(summary) +
      '">' +
      escapeHtml(prefix + summary) +
      "</span>"
    );
  }

  function flightCardClassForRank(rank) {
    const r = Number(rank) || 1;
    if (r === 1) return "flight-card best rank-1";
    return "flight-card best rank-" + r;
  }

  function buildChipLabels(f) {
    const labels = [];
    const has = function (t) {
      return labels.indexOf(t) !== -1;
    };
    if (f.smart_badge) labels.push(String(f.smart_badge));
    if (f.metric_is_best && !has("Best") && !has("Top pick") && !has("Fastest trip") && !has("Lowest price")) {
      labels.push("Best");
    }
    if (f.metric_is_cheapest && !has("Cheapest") && !has("Lowest price")) labels.push("Cheapest");
    if (f.metric_is_fastest && !has("Fastest") && !has("Fastest trip")) labels.push("Fastest");
    return labels;
  }

  function buildChipRowHtml(f) {
    const labels = buildChipLabels(f);
    if (!labels.length) return "";
    let chips = "";
    for (let i = 0; i < labels.length; i++) {
      chips += '<span class="result-badge">' + escapeHtml(labels[i]) + "</span>";
    }
    return '<div class="result-chip-row">' + chips + "</div>";
  }

  function buildFlightHeadlineHtml(f) {
    const summary = escapeHtml(String(f.airline_summary || f.out_airline || "Flight"));
    const logoUrl = f.airline_logo_url ? String(f.airline_logo_url).trim() : "";
    const logo =
      logoUrl !== ""
        ? '<img class="flight-airline-logo" src="' +
          escapeHtml(logoUrl) +
          '" alt="" width="38" height="38" loading="lazy" decoding="async" />'
        : "";
    return (
      '<div class="flight-headline">' +
      '<div class="flight-airline-block">' +
      '<div class="flight-airline-row">' +
      '<div class="flight-airline-title">' +
      logo +
      '<div class="flight-airline">' +
      summary +
      "</div></div>" +
      buildChipRowHtml(f) +
      "</div></div></div>"
    );
  }

  function buildJourneyLegHtml(seg) {
    const layovers = seg.layovers || [];
    const stopsLower = String(seg.stops_label || "").toLowerCase();
    const lineWrapClass =
      "journey-line-wrap" + (layovers.length ? " has-layovers" : " is-nonstop");
    let lineStops = "";
    for (let i = 0; i < layovers.length; i++) {
      const lay = layovers[i];
      const tone = escapeHtml(String(lay.tone || "neutral"));
      const left = escapeHtml(String(lay.line_position_pct != null ? lay.line_position_pct : 0));
      lineStops +=
        '<div class="journey-line-stop tone-' +
        tone +
        '" style="left: ' +
        left +
        '%;" data-rich-tooltip="' +
        buildLayoverTooltip(lay) +
        '" tabindex="0">' +
        '<span class="journey-line-stop-dot"></span>' +
        '<span class="journey-line-stop-card"><strong>' +
        escapeHtml(String(lay.code || "")) +
        "</strong><span>" +
        escapeHtml(String(lay.duration_label || "")) +
        "</span></span></div>";
    }
    const routeChip =
      stopsLower !== "nonstop" && seg.route_chip
        ? '<span class="smart-chip tone-neutral">' + escapeHtml(String(seg.route_chip)) + "</span>"
        : "";
    const logoUrl = seg.airline_logo_url ? String(seg.airline_logo_url).trim() : "";
    const logo =
      logoUrl !== ""
        ? '<img class="journey-airline-logo" src="' +
          escapeHtml(logoUrl) +
          '" alt="" width="20" height="20" loading="lazy" decoding="async" />'
        : "";
    return (
      '<section class="journey-leg">' +
      '<div class="journey-leg-head">' +
      "<div>" +
      '<div class="journey-label-row">' +
      '<span class="segment-badge">' +
      escapeHtml(String(seg.label || "")) +
      '</span><span class="journey-airline-name muted small">' +
      logo +
      "<span>" +
      escapeHtml(String(seg.airline || "")) +
      "</span></span></div>" +
      '<div class="journey-route">' +
      escapeHtml(String(seg.origin || "")) +
      " → " +
      escapeHtml(String(seg.destination || "")) +
      "</div></div>" +
      '<div class="journey-leg-chips">' +
      '<span class="smart-chip tone-time">' +
      escapeHtml(String(seg.time_chip || "")) +
      '</span><span class="smart-chip tone-neutral">' +
      escapeHtml(String(seg.stops_label || "")) +
      "</span>" +
      routeChip +
      "</div></div>" +
      '<div class="journey-timeline">' +
      '<div class="journey-stop">' +
      '<div class="journey-time">' +
      escapeHtml(String(seg.depart_time || "")) +
      '</div><div class="journey-day">' +
      escapeHtml(String(seg.depart_day || "")) +
      '</div><div class="journey-airport" data-rich-tooltip="' +
      escapeHtml(String(seg.origin_name || "") + " (" + String(seg.origin || "") + ")") +
      '" tabindex="0">' +
      escapeHtml(String(seg.origin || "")) +
      "</div></div>" +
      '<div class="journey-track">' +
      '<div class="' +
      lineWrapClass +
      '">' +
      '<div class="journey-duration">' +
      escapeHtml(String(seg.duration || "")) +
      '</div><div class="journey-line">' +
      lineStops +
      "</div></div></div>" +
      '<div class="journey-stop align-right">' +
      '<div class="journey-time">' +
      escapeHtml(String(seg.arrive_time || "")) +
      '</div><div class="journey-day">' +
      escapeHtml(String(seg.arrive_day || "")) +
      '</div><div class="journey-airport" data-rich-tooltip="' +
      escapeHtml(String(seg.destination_name || "") + " (" + String(seg.destination || "") + ")") +
      '" tabindex="0">' +
      escapeHtml(String(seg.destination || "")) +
      "</div></div></div></section>"
    );
  }

  function buildJourneyGridHtml(segments) {
    const n = segments.length;
    const gridClass = "journey-grid" + (n === 1 ? " single" : "");
    let inner = "";
    for (let i = 0; i < n; i++) {
      inner += buildJourneyLegHtml(segments[i]);
    }
    return '<div class="' + gridClass + '">' + inner + "</div>";
  }

  function buildMobileLayoverRow(seg) {
    const layovers = seg.layovers || [];
    if (!layovers.length) {
      return (
        '<div class="flight-mobile-time-center">' +
        '<div class="flight-mobile-time-line-wrap is-nonstop">' +
        '<span class="flight-mobile-time-line" aria-hidden="true"></span></div>' +
        '<span class="flight-mobile-leg-duration">' +
        escapeHtml(String(seg.duration || "")) +
        "</span></div>"
      );
    }
    const first = layovers[0];
    const codeShown =
      layovers.length === 1
        ? escapeHtml(String(first.code || ""))
        : escapeHtml(String(first.code || "")) + " + " + (layovers.length - 1);
    const layName =
      layovers.length === 1
        ? escapeHtml(String(first.name || first.code || ""))
        : escapeHtml(String(first.name || first.code || "")) +
          " + " +
          (layovers.length - 1) +
          " more stops";
    const layTime =
      layovers.length === 1
        ? escapeHtml(String(first.duration_label || ""))
        : escapeHtml(String(seg.stops_label || ""));
    return (
      '<div class="flight-mobile-time-center">' +
      '<div class="flight-mobile-layover">' +
      '<span class="flight-mobile-layover-airport">' +
      codeShown +
      '</span><div class="flight-mobile-layover-popover" role="tooltip">' +
      layName +
      '</div><span class="flight-mobile-layover-time">' +
      layTime +
      "</span></div>" +
      '<div class="flight-mobile-time-line-wrap has-layover">' +
      '<span class="flight-mobile-time-line" aria-hidden="true"></span></div>' +
      '<span class="flight-mobile-leg-duration">' +
      escapeHtml(String(seg.duration || "")) +
      "</span></div>"
    );
  }

  function buildMobileLegHtml(seg) {
    return (
      '<section class="flight-mobile-leg-summary">' +
      '<div class="flight-mobile-leg-top">' +
      '<span class="segment-badge">' +
      escapeHtml(String(seg.label || "")) +
      '</span><span class="flight-mobile-leg-copy">' +
      escapeHtml(String(seg.stops_label || "")) +
      "</span></div>" +
      '<div class="flight-mobile-leg-times">' +
      '<div class="flight-mobile-time-block">' +
      '<span class="flight-mobile-time">' +
      escapeHtml(String(seg.depart_time || "")) +
      '</span><div class="flight-mobile-time-meta">' +
      '<span class="flight-mobile-airport-code">' +
      escapeHtml(String(seg.origin || "")) +
      '</span><span class="flight-mobile-leg-copy">' +
      escapeHtml(String(seg.depart_day || "")) +
      "</span></div></div>" +
      buildMobileLayoverRow(seg) +
      '<div class="flight-mobile-time-block align-right">' +
      '<span class="flight-mobile-time">' +
      escapeHtml(String(seg.arrive_time || "")) +
      '</span><div class="flight-mobile-time-meta">' +
      '<span class="flight-mobile-airport-code">' +
      escapeHtml(String(seg.destination || "")) +
      '</span><span class="flight-mobile-leg-copy">' +
      escapeHtml(String(seg.arrive_day || "")) +
      "</span></div></div></div></section>"
    );
  }

  function buildFlightMobileLegsHtml(segments) {
    let html = "";
    for (let i = 0; i < segments.length; i++) {
      html += buildMobileLegHtml(segments[i]);
    }
    return '<div class="flight-mobile-legs">' + html + "</div>";
  }

  function buildFlightMobileSummaryHtml(f, primaryPriceLabel, secondaryNote) {
    const segments = f.segments_ui || [];
    const summary = escapeHtml(String(f.airline_summary || f.out_airline || "Flight"));
    const logoUrl = f.airline_logo_url ? String(f.airline_logo_url).trim() : "";
    const logo =
      logoUrl !== ""
        ? '<img class="flight-airline-logo flight-mobile-airline-logo" src="' +
          escapeHtml(logoUrl) +
          '" alt="" width="36" height="36" loading="lazy" decoding="async" />'
        : "";
    let badges = "";
    if (f.smart_badge) {
      badges += '<span class="result-badge">' + escapeHtml(String(f.smart_badge)) + "</span>";
    }
    return (
      '<div class="flight-mobile-summary">' +
      '<div class="flight-mobile-top">' +
      '<div class="flight-mobile-price-inline">' +
      '<span class="flight-mobile-price-caption">Total</span>' +
      '<span class="flight-mobile-price-amount" data-tier-price-mobile>' +
      escapeHtml(primaryPriceLabel) +
      '</span><span class="flight-mobile-price-note">' +
      escapeHtml(secondaryNote) +
      "</span></div></div>" +
      '<div class="flight-mobile-airline-row">' +
      logo +
      '<span class="flight-mobile-airline">' +
      summary +
      "</span>" +
      badges +
      "</div>" +
      buildFlightMobileLegsHtml(segments) +
      "</div>"
    );
  }

  function buildPriceComparisonCueHtml(f) {
    const delta = Number(f.price_vs_cheapest || 0);
    if (delta > 0.5) {
      const d = Number.isFinite(delta) ? delta.toFixed(2) : "0";
      return (
        '<div class="price-comparison-cue" data-price-compare-cue data-initial-delta="' +
        escapeHtml(d) +
        '">+$' +
        String(Math.round(delta)) +
        " more than cheapest in this search</div>"
      );
    }
    return (
      '<div class="price-comparison-cue" data-price-compare-cue data-initial-delta="0">Cheapest in this search</div>'
    );
  }

  function buildFlightSideHtml(f, primaryPriceLabel, secondaryNote) {
    return (
      '<aside class="flight-side">' +
      '<div class="price-panel">' +
      '<div class="price-kicker">Total</div>' +
      '<div class="price" data-tier-price-desktop>' +
      escapeHtml(primaryPriceLabel) +
      '</div><div class="price-meta muted small">' +
      '<div class="price-secondary-note">' +
      escapeHtml(secondaryNote) +
      "</div></div>" +
      buildPriceComparisonCueHtml(f) +
      "</div></aside>"
    );
  }

  function buildFcCarrierHtml(f) {
    const logoUrl = f.airline_logo_url ? String(f.airline_logo_url).trim() : "";
    const logo = logoUrl
      ? '<img class="fc-logo" src="' + escapeHtml(logoUrl) + '" alt="" width="38" height="38" loading="lazy" decoding="async" />'
      : '<div class="fc-logo-ph"></div>';
    const name = escapeHtml(String(f.airline_summary || f.out_airline || ""));
    const labels = buildChipLabels(f);
    const badges = labels.length
      ? '<div class="fc-badges">' + labels.map(function(l) { return '<span class="fc-badge">' + escapeHtml(l) + '</span>'; }).join("") + '</div>'
      : "";
    return (
      '<div class="fc-carrier">' +
      logo +
      '<div class="fc-carrier-meta">' +
      '<span class="fc-carrier-name">' + name + '</span>' +
      badges +
      '</div></div>'
    );
  }

  function buildFcSegHtml(seg, isLast) {
    const layovers = seg.layovers || [];
    const isNonstop = !layovers.length;
    const nsCls = isNonstop ? " is-nonstop" : "";
    const layoverSummary = buildFcLayoverSummaryHtml(layovers);
    let dots = "";
    for (let i = 0; i < layovers.length; i++) {
      const lay = layovers[i];
      const pos = lay.line_position_pct != null ? lay.line_position_pct : 0;
      dots += '<span class="fc-via-dot" style="left:' + pos + '%"></span>';
    }
    return (
      '<div class="fc-seg">' +
      '<div class="fc-leg-times">' +
      '<div class="fc-endpoint">' +
      '<span class="fc-time">' + escapeHtml(String(seg.depart_time || "")) + '</span>' +
      '<span class="fc-iata">' + escapeHtml(String(seg.origin || "")) + '</span>' +
      '<span class="fc-date-lbl">' + escapeHtml(String(seg.depart_day || "")) + '</span>' +
      '</div>' +
      '<div class="fc-via">' +
      '<span class="fc-via-dur"><span class="fc-via-dur-label">Travel</span>' + escapeHtml(String(seg.duration || "")) + '</span>' +
      '<div class="fc-via-line' + nsCls + '">' + dots + '</div>' +
      '<span class="fc-via-stops' + nsCls + '">' + escapeHtml(String(seg.stops_label || "")) + '</span>' +
      layoverSummary +
      '</div>' +
      '<div class="fc-endpoint fc-endpoint--end">' +
      '<span class="fc-time">' + escapeHtml(String(seg.arrive_time || "")) + '</span>' +
      '<span class="fc-iata">' + escapeHtml(String(seg.destination || "")) + '</span>' +
      '<span class="fc-date-lbl">' + escapeHtml(String(seg.arrive_day || "")) + '</span>' +
      '</div>' +
      '</div>' +
      '</div>' +
      (isLast ? "" : '<div class="fc-seg-div"></div>')
    );
  }

  function buildFcItineraryHtml(f) {
    const segments = Array.isArray(f.segments_ui) ? f.segments_ui : [];
    if (!segments.length) {
      const story = f.trip_story || "Itinerary details will appear when the full search completes.";
      return '<div class="fc-itinerary"><p class="muted small" style="margin:0;line-height:1.45;font-size:12px;">' + escapeHtml(story) + '</p></div>';
    }
    let segsHtml = "";
    for (let i = 0; i < segments.length; i++) {
      segsHtml += buildFcSegHtml(segments[i], i === segments.length - 1);
    }
    return '<div class="fc-itinerary">' + segsHtml + '</div>';
  }

  function buildFcPricingHtml(f, primaryPriceLabel, note) {
    const delta = Number(f.price_vs_cheapest || 0);
    const cueCls = delta > 0.5 ? "fc-price-cue" : "fc-price-cue fc-price-cue--low";
    const cueText = delta > 0.5 ? "+$" + Math.round(delta) + " vs cheapest" : "Lowest price";
    return (
      '<div class="fc-pricing">' +
      '<div class="fc-price-wrap">' +
      '<div class="fc-price-cap">Total</div>' +
      '<div class="fc-price-amt" data-tier-price-desktop>' + escapeHtml(primaryPriceLabel) + '</div>' +
      '<div class="fc-price-note">' + escapeHtml(note) + '</div>' +
      '<div class="' + cueCls + '" data-price-compare-cue data-initial-delta="' + escapeHtml(String(delta.toFixed(2))) + '">' + cueText + '</div>' +
      '</div>' +
      '</div>'
    );
  }

  function buildResultsFlightCardHtml(rank, f, secondaryNote) {
    const currency = f.currency || "USD";
    const primaryPriceLabel = formatMoneyExact(currency, f.price);
    const note = secondaryNote || "per traveler";
    const p = Number(f.price);
    const dataPrice = Number.isFinite(p) ? p.toFixed(2) : "0";
    const dur = f.total_duration_min != null ? String(f.total_duration_min) : "0";
    const stops = f.total_stop_count != null ? String(f.total_stop_count) : "0";
    const stopFilterCount = f.stop_filter_count != null ? String(f.stop_filter_count) : stops;
    const fd = f.first_depart_at != null ? String(f.first_depart_at) : "";
    const fa = f.final_arrive_at != null ? String(f.final_arrive_at) : "";
    return (
      '<article class="' + flightCardClassForRank(rank) + ' gf-stream-live"' +
      ' data-price="' + dataPrice + '"' +
      ' data-duration-min="' + escapeHtml(dur) + '"' +
      ' data-total-stops="' + escapeHtml(stops) + '"' +
      ' data-stop-filter-count="' + escapeHtml(stopFilterCount) + '"' +
      ' data-out-depart-iso="' + escapeHtml(fd) + '"' +
      ' data-out-arrive-iso="' + escapeHtml(fa) + '">' +
      '<div class="fc-row">' +
      buildFcCarrierHtml(f) +
      buildFcItineraryHtml(f) +
      buildFcPricingHtml(f, primaryPriceLabel, note) +
      '</div>' +
      '</article>'
    );
  }

  function ensureProvisionalWrap() {
    // No classic #resultsCards left to anchor next to — this classic-shaped
    // preview feature (see the cardsEl comment above) has nowhere to mount.
    if (!cardsEl) return null;
    if (provisionalWrap) return provisionalWrap;
    provisionalWrap = document.createElement("section");
    provisionalWrap.className = "results-provisional-wrap";
    provisionalWrap.setAttribute("aria-label", "Live flight previews");
    provisionalWrap.innerHTML =
      '<div id="resultsProvisionalCards" class="cards" aria-live="polite" aria-relevant="additions text"></div>';
    cardsEl.parentNode.insertBefore(provisionalWrap, cardsEl);
    return provisionalWrap;
  }

  function clearProvisionalRows() {
    provisionalRows.forEach(function (el) {
      if (el && el.parentNode) el.parentNode.removeChild(el);
    });
    provisionalRows.clear();
    if (provisionalWrap) {
      provisionalWrap.style.display = "none";
    }
  }

  let scanCount = 0;
  let standardHandoffStarted = false;

  function applyStandardHandoffChrome() {
    const lede = document.getElementById("resultsFlexStreamLede");
    if (lede) lede.setAttribute("hidden", "hidden");
    if (chipsEl) chipsEl.setAttribute("hidden", "hidden");
    const loader = document.getElementById("flexFlightLoader");
    if (loader) loader.classList.add("flex-flight-loader--handoff");
  }

  /* ── Inline error banner (replaces window.alert) ── */
  function showStreamError(msg) {
    let banner = document.getElementById("skairStreamErrorBanner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "skairStreamErrorBanner";
      banner.setAttribute("role", "alert");
      banner.style.cssText = [
        "position:fixed", "top:72px", "left:50%", "transform:translateX(-50%)",
        "background:#1e293b", "border:1px solid #334155", "border-radius:12px",
        "color:#e2e8f0", "font-size:14px", "line-height:1.5",
        "padding:14px 20px 14px 16px", "max-width:min(440px,90vw)",
        "box-shadow:0 8px 32px rgba(0,0,0,.45)", "z-index:9999",
        "display:flex", "gap:12px", "align-items:flex-start",
      ].join(";");
      document.body.appendChild(banner);
    }
    const homeHref = "/";
    banner.innerHTML = (
      '<svg style="flex-shrink:0;margin-top:1px" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#f87171" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>' +
      '<div><strong style="display:block;margin-bottom:4px;color:#f1f5f9">Something went wrong</strong>' +
      '<span style="color:#94a3b8">' + String(msg || "We hit an unexpected issue.") + '</span>' +
      '<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">' +
      '<a href="' + homeHref + '" style="display:inline-block;padding:6px 14px;background:#4f46e5;color:#fff;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600">New search</a>' +
      '<button onclick="window.location.reload()" style="display:inline-block;padding:6px 14px;background:transparent;border:1px solid #475569;color:#cbd5e1;border-radius:8px;font-size:13px;cursor:pointer">Try again</button>' +
      '</div></div>'
    );
  }

  function showSlowConnectionWarning() {
    if (statusEl) {
      statusEl.textContent = "Still searching — this is taking a little longer than usual…";
    }
    if (statusDetailEl) {
      statusDetailEl.textContent = "Your connection may be slow or the airline inventory is large. We're still working on it.";
    }
  }

  async function consumeNdjsonResponse(response) {
    if (!response.ok) {
      let msg = "We couldn't reach the search service right now. Please check your connection and try again.";
      try {
        const j = await response.json();
        if (j && j.error) msg = j.error;
      } catch (e) {
        /* ignore */
      }
      showStreamError(msg);
      return;
    }

    if (!response.body || !response.body.getReader) {
      showStreamError("Your browser doesn't support live search streaming. Please try a modern browser like Chrome or Safari.");
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        try {
          applyEvent(JSON.parse(line));
        } catch (e) {
          /* ignore */
        }
      }
    }
    const tail = buf.trim();
    if (tail) {
      try {
        applyEvent(JSON.parse(tail));
      } catch (e) {
        /* ignore */
      }
    }
  }

  async function runAiStandardHandoff(obj) {
    if (standardHandoffStarted) return;
    standardHandoffStarted = true;
    const handoffAbort = new AbortController();
    const handoffSlowTimer = setTimeout(showSlowConnectionWarning, 25000);
    const handoffHardTimer = setTimeout(function () {
      handoffAbort.abort();
      showStreamError("The flight search is taking too long. Please try a new search.");
    }, 90000);
    try {
      const fd = new FormData();
      const fields = obj.fields || [];
      for (let i = 0; i < fields.length; i++) {
        const row = fields[i];
        if (row && row.length >= 2) fd.append(String(row[0]), String(row[1]));
      }
      applyStandardHandoffChrome();
      if (statusEl) statusEl.textContent = "Loading your flights…";
      setStatusDetail("We matched dates from your request and are pulling live fares and itineraries.");
      const streamStandardUrl = new URL("/search/stream", window.location.origin).toString();
      const response = await fetch(streamStandardUrl, {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        signal: handoffAbort.signal,
        headers: {
          Accept: "application/x-ndjson",
          "X-Requested-With": "fetch",
        },
      });
      await consumeNdjsonResponse(response);
    } catch (e) {
      if (e && e.name === "AbortError") return;
      /* Streaming failed — fall back to a full POST form submit */
      try {
        const f = document.createElement("form");
        f.method = "POST";
        f.action = String(obj.action || "/search");
        f.style.display = "none";
        const fields = obj.fields || [];
        for (let i = 0; i < fields.length; i++) {
          const row = fields[i];
          if (!row || row.length < 2) continue;
          const inp = document.createElement("input");
          inp.type = "hidden";
          inp.name = String(row[0]);
          inp.value = String(row[1]);
          f.appendChild(inp);
        }
        document.body.appendChild(f);
        HTMLFormElement.prototype.submit.call(f);
      } catch (submitErr) {
        showStreamError("We lost the connection. Please try a new search.");
      }
    } finally {
      clearTimeout(handoffSlowTimer);
      clearTimeout(handoffHardTimer);
    }
  }

  function applyEvent(obj) {
    if (!obj || !obj.type) return;
    if (obj.type === "ai_standard_handoff") {
      void runAiStandardHandoff(obj);
      return;
    }
    if (obj.type === "standard_search") {
      const stage = String(obj.stage || "");
      if (stage === "fetch") {
        bumpProgress(0.12);
        if (statusEl) statusEl.textContent = "Querying live fares…";
        setStatusDetail("Checking airline and aggregator inventory for your itinerary.");
      } else if (stage === "ranking") {
        standardFlightTotal = Number(obj.count) || 0;
        bumpProgress(0.52);
        const n = standardFlightTotal;
        if (statusEl) {
          statusEl.textContent = n ? "Ranking " + n + " itineraries…" : "Ranking results…";
        }
        setStatusDetail("Applying smart ordering, fare comparison, and display polish.");
      }
      return;
    }
    if (obj.type === "start") {
      tripOneway = !!obj.trip_oneway;
      routeOrigin = String(obj.origin || routeOrigin || "").trim();
      routeDest = String(obj.destination || routeDest || "").trim();
      clearProvisionalRows();
      phase1Scans = 0;
      phase2Scans = 0;
      if (statusEl) {
        statusEl.textContent =
          routeOrigin && routeDest
            ? "Live pricing · " + routeOrigin + " → " + routeDest
            : "Live pricing — scanning your travel month";
      }
      const mo = obj.flex_month || "";
      const n = Number(obj.initial_sample_count) || 0;
      const days = Number(obj.month_candidate_days) || 0;
      setStatusDetail(
        "Requesting " +
          (n || "multiple") +
          " vendor quotes for " +
          (mo || "your month") +
          (days ? " (" + days + " eligible days)." : "."),
      );
      bumpProgress(0.1);
      return;
    }
    if (obj.type === "flex_cache_hit") {
      tripOneway = !!obj.trip_oneway;
      if (statusEl) statusEl.textContent = "Loading cached results…";
      bumpProgress(0.94);
      return;
    }
    if (obj.type === "phase") {
      const ph = Number(obj.phase) || 1;
      const planned = Number(obj.dates_planned) || 0;
      if (ph === 1) {
        datesPlanned1 = planned || datesPlanned1;
        bumpProgress(0.12);
      } else {
        datesPlanned2 = planned || datesPlanned2;
        bumpProgress(0.66);
      }
      if (statusEl) statusEl.textContent = ph === 2 ? "Refining best options…" : "Scanning dates…";
      return;
    }
    if (obj.type === "scan_hit") {
      scanCount += 1;
      const ph = obj.phase === 2 ? 2 : 1;
      const planned = ph === 2 ? datesPlanned2 || datesPlanned1 || scanCount : datesPlanned1 || scanCount;
      if (ph === 2) {
        phase2Scans += 1;
      } else {
        phase1Scans += 1;
      }
      const done = ph === 2 ? phase2Scans : phase1Scans;
      bumpProgress(scanProgressFrac(ph, done, planned));
      if (statusEl) statusEl.textContent = ph === 2 ? "Refining best options…" : "Scanning dates…";
      updateBestPill(obj.hit || {});
      return;
    }
    if (obj.type === "picked_dates") {
      const p = obj.scan_price_total;
      const c = obj.scan_currency || "USD";
      const mid = tripOneway ? "" : " → " + (obj.return_date || "");
      bumpProgress(0.84);
      if (statusEl) {
        statusEl.textContent =
          "Best window locked · " + (obj.depart_date || "") + mid + " · about " + formatMoney(c, p);
      }
      if (headDatesEl) {
        headDatesEl.textContent =
          (obj.depart_date || "") +
          (tripOneway ? "" : " – " + (obj.return_date || "")) +
          " · ~" +
          formatMoney(c, p);
      }
      try {
        sessionStorage.setItem("skair_flex_insight", JSON.stringify({
          cheapest_price: p ? Number(p) : null,
          cheapest_date: obj.depart_date || null,
          return_date: obj.return_date || null,
          ts: Date.now(),
        }));
      } catch (e) { /* ignore */ }
      return;
    }
    if (obj.type === "final_leg") {
      if (statusEl) statusEl.textContent = obj.label || "Building itineraries…";
      bumpProgress(0.86);
      return;
    }
    if (obj.type === "final_fetch_start") {
      clearProvisionalRows();
      bumpProgress(0.88);
      if (statusEl) statusEl.textContent = "Loading full itineraries…";
      return;
    }
    if (obj.type === "provisional_flight") {
      const wrap = ensureProvisionalWrap();
      if (!wrap) return;
      wrap.style.display = "";
      const bucket = document.getElementById("resultsProvisionalCards");
      if (!bucket) return;
      const rank = Number(obj.rank) || 1;
      const f = obj.flight || {};
      const html = buildResultsFlightCardHtml(rank, f, "Estimate while checking dates");
      const holder = document.createElement("div");
      holder.innerHTML = html.trim();
      const nextNode = holder.firstElementChild;
      if (!nextNode) return;
      const existing = provisionalRows.get(rank);
      if (existing && existing.parentNode) {
        existing.parentNode.replaceChild(nextNode, existing);
      } else {
        bucket.appendChild(nextNode);
      }
      provisionalRows.set(rank, nextNode);
      return;
    }
    if (obj.type === "offers_received") {
      bumpProgress(0.91);
      if (statusEl)
        statusEl.textContent =
          "Ranking " + obj.count + " itineraries…";
      return;
    }
    if (obj.type === "flex_fallback_try") {
      bumpProgress(0.9);
      if (statusEl) statusEl.textContent = "Surfacing best available fare…";
      return;
    }
    if (obj.type === "sorting") {
      bumpProgress(0.93);
      if (statusEl) statusEl.textContent = "Almost ready…";
      return;
    }
    if (obj.type === "fallback_notice") {
      if (statusEl) statusEl.textContent = obj.message || "";
      return;
    }
    if (obj.type === "flight_row") {
      const rank = Number(obj.rank) || 1;
      if (isStandardSearchStream || standardHandoffStarted) {
        const total = standardFlightTotal || skeletonSlots.length || 12;
        bumpProgress(0.16 + 0.72 * Math.min(1, rank / Math.max(total, 1)));
      } else {
        bumpProgress(Math.max(loadProgress, 0.93 + Math.min(0.04, rank * 0.008)));
      }
      if (!classicHidden) {
        const f = obj.flight || {};
        const html = buildResultsFlightCardHtml(rank, f, "per traveler");
        const idx = rank - 1;
        if (idx >= 0 && idx < skeletonSlots.length && skeletonSlots[idx]) {
          const slot = skeletonSlots[idx];
          const wrap = document.createElement("div");
          wrap.innerHTML = html.trim();
          const node = wrap.firstElementChild;
          if (node && slot.parentNode) slot.parentNode.replaceChild(node, slot);
          skeletonSlots[idx] = null;
        } else {
          cardsEl.insertAdjacentHTML("beforeend", html);
        }
      }
      return;
    }
    if (obj.type === "complete") {
      bumpProgress(0.995);
      const reloadUrl = typeof obj.url === "string" && obj.url ? obj.url : "/search";
      swapInFinalResults(obj.html, reloadUrl);
      return;
    }
    if (obj.type === "error") {
      showStreamError(obj.message || "The search couldn't complete. Please try a new search.");
    }
  }

  async function run() {
    const fd = new FormData(bootForm);
    const modeFd = String(fd.get("mode") || "").toLowerCase();
    const hasFlexOrigin = String(fd.get("origin") || "").trim() !== "";
    const o = String(fd.get("origin") || "").trim().toUpperCase();
    const d = String(fd.get("destination") || "").trim().toUpperCase();

    /* Save search in the format that restoreSearch() on the homepage understands */
    try {
      const modeFd = String(fd.get("mode") || "").toLowerCase();
      let saveData;
      if (modeFd === "ai") {
        saveData = { mode: "ai", ai_text: String(fd.get("ai_text") || ""), ts: Date.now() };
      } else {
        saveData = {
          mode:        "manual",
          ts:          Date.now(),
          origin:      String(fd.get("origin")      || ""),
          destination: String(fd.get("destination") || ""),
          depart_date: String(fd.get("depart_date") || ""),
          return_date: String(fd.get("return_date") || ""),
          cabin:       String(fd.get("cabin")       || ""),
          passengers:  String(fd.get("passengers")  || ""),
          trip_type:   String(fd.get("trip_type")   || ""),
          flex_month:  String(fd.get("flex_month")  || ""),
          nonstop:     String(fd.get("nonstop")     || ""),
        };
      }
      sessionStorage.setItem("skair_last_search", JSON.stringify(saveData));
    } catch (e) { /* ignore */ }

    if (isStandardSearchStream) {
      standardFlightTotal = 0;
      if (modeFd === "ai") {
        if (statusEl) statusEl.textContent = "Understanding your trip…";
        setStatusDetail("AI is aligning your request with live fare sources.");
      } else if (o && d) {
        routeOrigin = o;
        routeDest = d;
        if (statusEl) statusEl.textContent = "Live search · " + o + " → " + d;
        setStatusDetail("Streaming ranked options as each itinerary is priced.");
      } else {
        if (statusEl) statusEl.textContent = "Searching live inventory…";
        setStatusDetail("Fetching itineraries from airline partners.");
      }
      bumpProgress(0.08);
    } else if (modeFd === "ai" && !hasFlexOrigin) {
      if (statusEl) statusEl.textContent = "Parsing your trip with AI…";
      setStatusDetail("Extracting airports, trip length, cabin, and month from your text.");
    } else if (o && d) {
      routeOrigin = o;
      routeDest = d;
      if (statusEl) statusEl.textContent = "Opening live radar · " + o + " → " + d;
      setStatusDetail("Connecting to pricing sources for your flexible-month request.");
      bumpProgress(0.06);
    } else if (statusEl) {
      statusEl.textContent = "Starting flexible-date search…";
      setStatusDetail("Negotiating the first live quotes with airline partners.");
      bumpProgress(0.06);
    }

    /* Slow-connection warning after 25 s, hard timeout after 90 s */
    const abortCtrl = new AbortController();
    const slowTimer = setTimeout(showSlowConnectionWarning, 25000);
    const hardTimer = setTimeout(function () {
      abortCtrl.abort();
      showStreamError(
        "The search is taking longer than expected — this can happen on slow connections or with complex routes. " +
        "Please try again or use the manual search for faster results."
      );
    }, 90000);

    try {
      const response = await fetch(streamUrl, {
        method: "POST",
        body: fd,
        credentials: "same-origin",
        signal: abortCtrl.signal,
        headers: {
          Accept: "application/x-ndjson",
          "X-Requested-With": "fetch",
        },
      });
      await consumeNdjsonResponse(response);
    } catch (e) {
      if (e && e.name !== "AbortError") {
        showStreamError(
          "We lost the connection mid-search. Please check your internet and try again."
        );
      }
    } finally {
      clearTimeout(slowTimer);
      clearTimeout(hardTimer);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run, { once: true });
  } else {
    run();
  }
})();
