/* Skairova Flight Results — "Lab" experience client behavior (v2).
   Reads a JSON payload embedded by results_lab_body.html (the same
   `flights` the classic page renders — no separate fetch) and owns all
   rendering: the departure -> return -> review staging for round trips is
   a purely client-side grouping over that same bundled result set, so it
   can never disagree with what the backend actually returned. */
(function () {
  "use strict";

  var root = document.getElementById("resultsLabBody");
  if (!root) return;
  var dataEl = document.getElementById("labFlightsData");
  if (!dataEl) return;

  var payload;
  try {
    payload = JSON.parse(dataEl.textContent || "{}");
  } catch (e) {
    return;
  }
  var allFlights = payload.flights || [];
  var searchForm = payload.searchForm || null;
  if (!allFlights.length) return; // empty/error state already rendered server-side

  var isRoundTrip = payload.tripMode === "roundtrip" && allFlights.every(function (f) { return f.out && f.ret; });

  /* ---------------- formatting helpers ---------------- */

  function money(n) { return "$" + Math.round(n).toLocaleString(); }
  function stripCents(label) { return (label || "").replace(/\.00$/, ""); }
  function hm(mins) {
    mins = Math.max(0, Math.round(mins || 0));
    var h = Math.floor(mins / 60), m = mins % 60;
    return h + "h" + (m ? " " + m + "m" : "");
  }
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function hourOf(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    return isNaN(d.getTime()) ? null : d.getHours();
  }
  function timeBucket(h) {
    if (h === null) return null;
    if (h >= 5 && h < 12) return "morning";
    if (h >= 12 && h < 18) return "afternoon";
    if (h >= 18 && h < 22) return "evening";
    return "night";
  }
  function stopBucket(stops) {
    if (stops <= 0) return 0;
    if (stops === 1) return 1;
    return 2;
  }
  /* "Emirates + flydubai" -> ["Emirates", "flydubai"] so the airline filter
     lists individual carriers rather than one checkbox per combination. */
  function airlineTokens(name) {
    return (name || "").split(/\s*\+\s*/).filter(Boolean);
  }

  /* ---------------- explicit, independently-testable rank derivations ----------------
     Each takes the current stage's item array and returns one item (or null).
     None of these read DOM/module state, so they're copy-paste-portable into a
     plain Node test — see scratchpad/check_lab_derivations.js. */
  function getBestItem(items) {
    return items.length ? items[0] : null; // server-recommended order for this stage
  }
  function getCheapestItem(items) {
    if (!items.length) return null;
    return items.reduce(function (a, b) { return b.price < a.price ? b : a; }, items[0]);
  }
  function getFastestItem(items) {
    if (!items.length) return null;
    return items.reduce(function (a, b) { return b.duration < a.duration ? b : a; }, items[0]);
  }
  function getBestDirectItem(items) {
    var nonstop = items.filter(function (i) { return i.stops === 0; });
    if (!nonstop.length) return null;
    return nonstop.reduce(function (a, b) { return b.price < a.price ? b : a; }, nonstop[0]);
  }

  /* ---------------- DOM refs ---------------- */

  var listEl = document.getElementById("labList");
  var progressEl = document.getElementById("labProgress");
  var pinnedEl = document.getElementById("labPinnedDeparture");
  var visibleCountEl = document.getElementById("labVisibleCount");
  var countLabelEl = document.getElementById("labResultsCountLabel");
  var noMatchEl = document.getElementById("labNoMatch");
  var tabsWrap = root.querySelector(".lab-tabs");
  var quickRow = root.querySelector(".lab-quickrow");
  var railEl = document.getElementById("labRail");
  var layoutEl = root.querySelector(".lab-layout");
  var resultsHeaderEl = document.getElementById("labResultsHeader");

  var stopInputs = {
    0: root.querySelector('[data-lab-filter-stop="0"]'),
    1: root.querySelector('[data-lab-filter-stop="1"]'),
    2: root.querySelector('[data-lab-filter-stop="2"]'),
  };
  var departInputs = {
    morning: root.querySelector('[data-lab-filter-depart="morning"]'),
    afternoon: root.querySelector('[data-lab-filter-depart="afternoon"]'),
    evening: root.querySelector('[data-lab-filter-depart="evening"]'),
    night: root.querySelector('[data-lab-filter-depart="night"]'),
  };
  var durationSlider = document.getElementById("labDurationMax");
  var durationLabel = document.getElementById("labDurationMaxLabel");
  var priceSlider = document.getElementById("labPriceMax");
  var priceLabel = document.getElementById("labPriceMaxLabel");
  var airlineListEl = document.getElementById("labAirlineFilterList");
  var layoverListEl = document.getElementById("labLayoverFilterList");
  var nonstopQuick = root.querySelector('[data-lab-quick="nonstop"]');
  var stop1Quick = root.querySelector('[data-lab-quick="stop1"]');

  /* ---------------- flow state ---------------- */

  function outboundKey(f) {
    return [f.out.originCode, f.out.destCode, f.out.departIso || f.out.departTime, f.out.durationMin, f.out.stops, f.airlineName].join("|");
  }

  var departureGroups = [];
  if (isRoundTrip) {
    var byKey = new Map();
    allFlights.forEach(function (f) {
      var key = outboundKey(f);
      var g = byKey.get(key);
      if (!g) {
        g = { key: key, out: f.out, airlineName: f.airlineName, airlineLogoUrl: f.airlineLogoUrl, airlineMix: f.airlineMix, members: [], best: f };
        byKey.set(key, g);
        departureGroups.push(g);
      }
      g.members.push(f);
      if (f.price < g.best.price) g.best = f;
    });
  }

  var flow = {
    stage: isRoundTrip ? "departure" : "flat",
    chosenGroup: null,
    chosenFlight: null,
  };

  var filterState = {
    stops: { 0: false, 1: false, 2: false },
    depart: { morning: false, afternoon: false, evening: false, night: false },
    maxDuration: Infinity,
    maxPrice: Infinity,
    airlines: {},
    layovers: {},
  };

  function anyChecked(map) {
    return Object.keys(map).some(function (k) { return map[k]; });
  }

  /* ---------------- generic "current stage items" abstraction ---------------- */
  /* Every stage reduces to a flat array of { key, price, duration, stops,
     departIso, airlineName, layovers, ref } so tabs/filters are written once. */

  function itemsForStage() {
    if (flow.stage === "departure") {
      return departureGroups.map(function (g) {
        return {
          key: g.key, price: g.best.price, duration: g.out.durationMin, stops: g.out.stops,
          departIso: g.out.departIso, airlineName: g.airlineName, layovers: g.out.layovers, ref: g,
        };
      });
    }
    if (flow.stage === "return" && flow.chosenGroup) {
      return flow.chosenGroup.members.map(function (f) {
        return {
          key: f.id, price: f.price, duration: f.ret.durationMin, stops: f.ret.stops,
          departIso: f.ret.departIso, airlineName: f.airlineName, layovers: f.ret.layovers, ref: f,
        };
      });
    }
    if (flow.stage === "flat") {
      return allFlights.map(function (f) {
        return {
          key: f.id, price: f.price, duration: f.totalDurationMin, stops: f.totalStops,
          departIso: f.out ? f.out.departIso : "", airlineName: f.airlineName,
          layovers: (f.out ? f.out.layovers : []).concat(f.ret ? f.ret.layovers : []), ref: f,
        };
      });
    }
    return [];
  }

  /* ---------------- card templates ---------------- */

  /* `showLabel` only true when a card shows 2 legs at once (review/flat) —
     a single-leg departure/return card already has its meaning established
     by the booking-progress stepper and pinned-departure bar, so repeating
     "Departure"/"Outbound" on the card itself is pure noise (spec #13). */
  function legRow(leg, showLabel) {
    var stopsClass = leg.stops === 0 ? "is-nonstop" : (leg.stops >= 2 ? "is-warn" : "");
    var mid = leg.stops === 1 && leg.layovers.length
      ? leg.stopsLabel + " &middot; " + esc(leg.layovers[0].code)
      : leg.stopsLabel;
    return (
      '<div class="lab-leg">' +
        (showLabel && leg.label ? '<span class="lab-leg-label">' + esc(leg.label) + "</span>" : "") +
        '<div class="lab-leg-point">' +
          '<span class="lab-leg-time">' + esc(leg.departTime) + "</span>" +
          '<span class="lab-leg-iata">' + esc(leg.originCode) + '<span class="lab-leg-day">' + esc(leg.departDay) + "</span></span>" +
        "</div>" +
        '<div class="lab-leg-mid">' +
          '<span class="lab-leg-duration">' + esc(leg.durationLabel) + "</span>" +
          '<div class="lab-leg-line"></div>' +
          '<span class="lab-leg-stops ' + stopsClass + '">' + mid + "</span>" +
        "</div>" +
        '<div class="lab-leg-point lab-leg-point--end">' +
          '<span class="lab-leg-time">' + esc(leg.arriveTime) + "</span>" +
          '<span class="lab-leg-iata">' + esc(leg.destCode) + '<span class="lab-leg-day">' + esc(leg.arriveDay) + "</span></span>" +
        "</div>" +
      "</div>"
    );
  }

  /* Stop status has exactly one home: the journey line itself (legRow's mid
     lane). This row is baggage + material-risk warnings only — no repeated
     "Nonstop"/"N stops" (spec #17). Unknown baggage is never a green check
     (this app has no fare-bundle baggage data at itinerary level). */
  function journeyMeta(warnings) {
    var bits = ['<span class="lab-meta-item lab-meta-item--muted">Baggage: details at fare selection</span>'];
    (warnings || []).forEach(function (w) {
      bits.push('<span class="lab-meta-item lab-meta-item--warn">⚠ ' + esc(w) + "</span>");
    });
    return '<div class="lab-journey-meta">' + bits.join("") + "</div>";
  }

  var BADGE_DISPLAY = { "Top pick": "Best value", "Lowest price": "Cheapest" };

  function badgeRow(badge, reasoning) {
    if (!badge) return "";
    var isTop = badge === "Top pick";
    var label = BADGE_DISPLAY[badge] || badge;
    return (
      '<div class="lab-card-badges">' +
        '<span class="lab-badge ' + (isTop ? "lab-badge--match" : "lab-badge--rank") + '">' +
          esc(label) +
          (reasoning ? '<button type="button" class="lab-badge-why" data-lab-why data-reason="' + esc(reasoning) + '" aria-label="Why this is ranked here"><svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4.5M12 8h.01" stroke-linecap="round"/></svg></button>' : "") +
        "</span>" +
      "</div>"
    );
  }

  function detailsPanel(legs) {
    var html = "";
    var showLabel = legs.length > 1;
    legs.forEach(function (leg) {
      html += "<div>";
      if (showLabel && leg.label) html += '<div class="lab-details-leg-title">' + esc(leg.label) + "</div>";
      html += '<div class="lab-timeline">';
      html += '<div class="lab-timeline-row"><div class="lab-timeline-dot"></div><div class="lab-timeline-body">' +
        '<div class="lab-timeline-time">' + esc(leg.departTime) + " &middot; " + esc(leg.departDay) + "</div>" +
        '<div class="lab-timeline-place">' + esc(leg.originCode) + (leg.originName ? " &middot; " + esc(leg.originName) : "") + "</div>" +
      "</div></div>";
      html += '<div class="lab-timeline-row"><div class="lab-timeline-track"><div class="lab-timeline-track-line"></div></div><div class="lab-timeline-body">' +
        '<span class="lab-timeline-place">' + esc(leg.durationLabel) + " in the air</span>";
      (leg.layovers || []).forEach(function (lay) {
        html += '<div class="lab-timeline-layover">Layover &middot; ' + esc(lay.code) + (lay.name ? " &middot; " + esc(lay.name) : "") + (lay.durationLabel ? " &middot; " + esc(lay.durationLabel) : "") + "</div>";
      });
      html += "</div></div>";
      html += '<div class="lab-timeline-row"><div class="lab-timeline-dot"></div><div class="lab-timeline-body">' +
        '<div class="lab-timeline-time">' + esc(leg.arriveTime) + " &middot; " + esc(leg.arriveDay) + "</div>" +
        '<div class="lab-timeline-place">' + esc(leg.destCode) + (leg.destName ? " &middot; " + esc(leg.destName) : "") + "</div>" +
      "</div></div>";
      html += "</div></div>";
    });
    return html;
  }

  /* `mix` (e.g. "Operated by British Airways") only makes sense when the
     card is describing a single itinerary end-to-end. On a departure-only
     or return-only card there is no "both ways" to describe — showing it
     there is exactly the leaked copy spec #14 calls out, so callers must
     pass an empty string for single-leg cards. */
  function carrierBlock(name, logoUrl, mix) {
    var showMix = mix && mix.toLowerCase().indexOf("single") === -1 && mix.toLowerCase().indexOf("same airline") === -1;
    return (
      '<div class="lab-carrier">' +
        (logoUrl ? '<img class="lab-carrier-logo" src="' + esc(logoUrl) + '" alt="' + esc(name) + '" width="30" height="30" loading="lazy" decoding="async" />' : '<div class="lab-carrier-logo-ph" aria-hidden="true"></div>') +
        '<div class="lab-carrier-text">' +
          '<span class="lab-carrier-name">' + esc(name) + "</span>" +
          (showMix ? '<span class="lab-carrier-mix">' + esc(mix) + "</span>" : "") +
        "</div>" +
      "</div>"
    );
  }

  /* "round trip · per traveler" / "one way · total for 3 travelers" — the
     one qualifier line every card uses under the price (spec #20: never
     the words "Total trip" as a label). */
  function tripQualifier(pax) {
    var tripWord = isRoundTrip ? "round trip" : "one way";
    var paxWord = pax > 1 ? "total for " + pax + " travelers" : "per traveler";
    return tripWord + " · " + paxWord;
  }

  /* Renders one card. `kind` = "departure" | "return" | "review" | "flat". */
  function cardHtml(kind, entity) {
    var legs, priceAmt, priceQualifier, secondaryNote, cueHtml, ctaHtml, badge, reasoning, key, airlineName, logoUrl, mix;

    if (kind === "departure") {
      var g = entity;
      legs = [g.out];
      priceAmt = money(g.best.price);
      priceQualifier = tripQualifier(g.best.pax);
      secondaryNote = "";
      cueHtml = "";
      badge = g.best.badge; reasoning = g.best.badgeReasoning;
      key = g.key; airlineName = g.airlineName; logoUrl = g.airlineLogoUrl; mix = "";
      ctaHtml = '<button type="button" class="lab-select-btn" data-lab-choose-departure="' + esc(g.key) + '">Select</button>';
    } else if (kind === "return") {
      var f = entity;
      var base = flow.chosenGroup.best.price;
      var delta = f.price - base;
      legs = [f.ret];
      if (delta > 0.5) {
        priceAmt = "+" + money(delta);
        priceQualifier = "vs. this departure's cheapest return";
        secondaryNote = "Round trip total " + stripCents(f.priceLabel);
      } else {
        priceAmt = stripCents(f.priceLabel);
        priceQualifier = tripQualifier(f.pax);
        secondaryNote = "";
      }
      cueHtml = "";
      badge = f.badge; reasoning = f.badgeReasoning;
      key = f.id; airlineName = f.airlineName; logoUrl = f.airlineLogoUrl; mix = "";
      ctaHtml = '<button type="button" class="lab-select-btn" data-lab-choose-return="' + esc(f.id) + '">Select</button>';
    } else {
      // "flat" (one-way, or fallback) and "review" both show the full itinerary.
      var fl = entity;
      legs = fl.ret ? [fl.out, fl.ret] : [fl.out];
      priceAmt = stripCents(fl.priceLabel);
      priceQualifier = tripQualifier(fl.pax);
      secondaryNote = "";
      cueHtml = fl.priceVsCheapest > 0.5
        ? '<span class="lab-price-cue">+' + money(fl.priceVsCheapest) + " vs cheapest</span>"
        : '<span class="lab-price-cue is-low">Lowest price</span>';
      badge = fl.badge; reasoning = fl.badgeReasoning;
      key = fl.id; airlineName = fl.airlineName; logoUrl = fl.airlineLogoUrl; mix = fl.airlineMix;
      ctaHtml = fl.checkoutUrl
        ? '<a class="lab-select-btn" href="' + esc(fl.checkoutUrl) + '"' + (fl.tripCombo ? "" : ' target="_blank" rel="noopener noreferrer"') + ' data-result-id="' + esc(fl.id) + '">Select</a>'
        : '<span class="lab-select-btn" aria-disabled="true">Unavailable</span>';
    }

    var showLegLabel = legs.length > 1;

    return (
      '<article class="lab-card" data-key="' + esc(key) + '">' +
        badgeRow(badge, reasoning) +
        '<div class="lab-card-main">' +
          carrierBlock(airlineName, logoUrl, mix) +
          '<div class="lab-itinerary">' +
            legs.map(function (l) { return legRow(l, showLegLabel); }).join("") +
            journeyMeta([]) +
          "</div>" +
          '<div class="lab-pricing">' +
            '<div class="lab-pricing-info">' +
              '<span class="lab-price-amt">' + priceAmt + "</span>" +
              '<span class="lab-price-note">' + esc(priceQualifier) + "</span>" +
              (secondaryNote ? '<span class="lab-price-note">' + esc(secondaryNote) + "</span>" : "") +
              '<span class="lab-price-note lab-price-note--taxes">Taxes &amp; fees included</span>' +
              cueHtml +
            "</div>" +
            ctaHtml +
          "</div>" +
        "</div>" +
        '<div class="lab-card-footer">' +
          '<button type="button" class="lab-details-toggle" aria-expanded="false" data-lab-details-toggle>Flight details' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>' +
          "</button>" +
        "</div>" +
        '<div class="lab-details" hidden data-lab-details>' + detailsPanel(legs) + "</div>" +
      "</article>"
    );
  }

  /* ---------------- filtering + sorting ---------------- */

  function passesFilters(item) {
    if (anyChecked(filterState.stops) && !filterState.stops[stopBucket(item.stops)]) return false;
    var bucket = timeBucket(hourOf(item.departIso));
    if (bucket && anyChecked(filterState.depart) && !filterState.depart[bucket]) return false;
    if (item.duration > filterState.maxDuration) return false;
    if (item.price > filterState.maxPrice) return false;
    if (anyChecked(filterState.airlines) && !airlineTokens(item.airlineName).some(function (n) { return filterState.airlines[n]; })) return false;
    if (anyChecked(filterState.layovers) && item.layovers.length && !item.layovers.some(function (l) { return filterState.layovers[l.code]; })) return false;
    if (anyChecked(filterState.layovers) && !item.layovers.length && item.stops > 0) return false;
    return true;
  }

  var activeTab = "best";

  function sortedVisible(items) {
    var visible = items.filter(passesFilters);
    if (activeTab === "direct") visible = visible.filter(function (it) { return it.stops === 0; });
    if (activeTab === "cheapest") visible.sort(function (a, b) { return a.price - b.price || a.duration - b.duration; });
    else if (activeTab === "fastest") visible.sort(function (a, b) { return a.duration - b.duration || a.price - b.price; });
    else if (activeTab === "direct") visible.sort(function (a, b) { return a.price - b.price; });
    // "best" keeps original (server-recommended) order — items arrays are
    // already built in that order for every stage.
    return visible;
  }

  /* ---------------- render orchestration ---------------- */

  function rebuildRailBounds(items) {
    if (!items.length) return;
    var durations = items.map(function (i) { return i.duration; });
    var prices = items.map(function (i) { return i.price; });
    var dMax = Math.max.apply(null, durations);
    var pMax = Math.max.apply(null, prices);
    var pMin = Math.min.apply(null, prices);
    durationSlider.min = "0";
    durationSlider.max = String(Math.max(dMax, 60));
    durationSlider.value = String(dMax);
    filterState.maxDuration = dMax;
    durationLabel.textContent = "Up to " + hm(dMax);

    priceSlider.min = String(Math.floor(pMin));
    priceSlider.max = String(Math.ceil(pMax));
    priceSlider.value = String(Math.ceil(pMax));
    filterState.maxPrice = Math.ceil(pMax);
    priceLabel.textContent = "Up to " + money(pMax);

    var airlineNames = Array.from(new Set(items.reduce(function (acc, i) { return acc.concat(airlineTokens(i.airlineName)); }, []))).sort();
    filterState.airlines = {};
    airlineListEl.innerHTML = "";
    airlineNames.forEach(function (name) {
      filterState.airlines[name] = false;
      var label = document.createElement("label");
      label.className = "lab-check";
      label.innerHTML = '<input type="checkbox" data-lab-filter-airline="' + esc(name) + '" /> <span></span>';
      label.querySelector("span").textContent = name;
      airlineListEl.appendChild(label);
    });
    Array.from(airlineListEl.querySelectorAll("[data-lab-filter-airline]")).forEach(function (input) {
      input.addEventListener("change", function () {
        filterState.airlines[input.dataset.labFilterAirline] = input.checked;
        renderStage();
      });
    });

    var layoverCodes = Array.from(new Set(items.reduce(function (acc, i) { return acc.concat(i.layovers.map(function (l) { return l.code; })); }, []))).sort();
    filterState.layovers = {};
    layoverListEl.innerHTML = "";
    var layoverGroup = layoverListEl.closest(".lab-rail-group");
    layoverGroup.hidden = layoverCodes.length === 0;
    layoverCodes.forEach(function (code) {
      filterState.layovers[code] = false;
      var label = document.createElement("label");
      label.className = "lab-check";
      label.innerHTML = '<input type="checkbox" data-lab-filter-layover="' + esc(code) + '" /> <span></span>';
      label.querySelector("span").textContent = code.toUpperCase();
      layoverListEl.appendChild(label);
    });
    Array.from(layoverListEl.querySelectorAll("[data-lab-filter-layover]")).forEach(function (input) {
      input.addEventListener("change", function () {
        filterState.layovers[input.dataset.labFilterLayover] = input.checked;
        renderStage();
      });
    });
  }

  function updateStopPriceLabels(items) {
    [0, 1, 2].forEach(function (bucket) {
      var without = items.filter(function (i) {
        if (stopBucket(i.stops) !== bucket) return false;
        var saved = filterState.stops[bucket];
        filterState.stops[bucket] = false;
        var ok = passesFilters(i) || !anyChecked(filterState.stops);
        filterState.stops[bucket] = saved;
        return true;
      });
      var el = root.querySelector('[data-lab-price-for-stop="' + bucket + '"]');
      if (!el) return;
      var candidates = without.filter(function (i) {
        var bkt = timeBucket(hourOf(i.departIso));
        if (bkt && anyChecked(filterState.depart) && !filterState.depart[bkt]) return false;
        if (i.duration > filterState.maxDuration) return false;
        if (i.price > filterState.maxPrice) return false;
        if (anyChecked(filterState.airlines) && !airlineTokens(i.airlineName).some(function (n) { return filterState.airlines[n]; })) return false;
        return true;
      });
      el.textContent = candidates.length ? "from " + money(Math.min.apply(null, candidates.map(function (i) { return i.price; }))) : "";
    });
  }

  function setTabValue(tab, item) {
    var el = root.querySelector('[data-lab-tab-value="' + tab + '"]');
    if (!el) return;
    el.textContent = item ? money(item.price) + " · " + hm(item.duration) : "—";
  }

  function updateTabSummaries(items) {
    if (!items.length) return;
    var direct = getBestDirectItem(items);
    setTabValue("best", getBestItem(items));
    setTabValue("cheapest", getCheapestItem(items));
    setTabValue("fastest", getFastestItem(items));
    setTabValue("direct", direct);
    var directTabBtn = root.querySelector('[data-lab-tab="direct"]');
    directTabBtn.disabled = !direct;
    directTabBtn.classList.toggle("is-unavailable", !direct);
  }

  function updateProgress() {
    if (!isRoundTrip) { progressEl.hidden = true; return; }
    progressEl.hidden = false;
    var order = ["departure", "return", "review"];
    var currentIdx = order.indexOf(flow.stage);
    Array.from(progressEl.querySelectorAll("[data-lab-step]")).forEach(function (stepEl) {
      var idx = order.indexOf(stepEl.dataset.labStep);
      stepEl.classList.toggle("is-current", idx === currentIdx);
      stepEl.classList.toggle("is-done", idx < currentIdx);
      if (idx === currentIdx) stepEl.setAttribute("aria-current", "step");
      else stepEl.removeAttribute("aria-current");
      var dot = stepEl.querySelector(".lab-progress-dot");
      dot.textContent = idx < currentIdx ? "✓" : String(idx + 1);
    });
  }

  function updatePinned() {
    if (flow.stage === "return" || flow.stage === "review") {
      var g = flow.chosenGroup;
      pinnedEl.hidden = false;
      pinnedEl.innerHTML =
        '<div class="lab-pinned-inner">' +
          '<span class="lab-pinned-label">Departure</span>' +
          '<span class="lab-pinned-route">' + esc(g.out.departTime) + " " + esc(g.out.originCode) + " → " + esc(g.out.arriveTime) + " " + esc(g.out.destCode) + "</span>" +
          '<span class="lab-pinned-meta">' + esc(g.airlineName) + " · " + esc(g.out.stopsLabel) + "</span>" +
          '<button type="button" class="lab-pinned-change" data-lab-go-departure>Change</button>' +
        "</div>";
    } else {
      pinnedEl.hidden = true;
    }
  }

  function renderStage() {
    updateProgress();
    updatePinned();

    if (flow.stage === "review") {
      // Hide the rail + header specifically, NOT the whole .lab-layout —
      // #labList (where the review card itself renders) lives inside
      // .lab-layout too, so hiding that container hid the review card
      // along with the filters it was meant to remove.
      tabsWrap.hidden = true;
      quickRow.hidden = true;
      railEl.hidden = true;
      resultsHeaderEl.hidden = true;
      layoutEl.classList.add("lab-layout--single");
      noMatchEl.hidden = true;
      listEl.innerHTML = cardHtml("review", flow.chosenFlight) +
        '<div class="lab-review-actions"><button type="button" class="lab-clear-filters" data-lab-go-return>← Change return</button></div>';
      wireCardEvents();
      return;
    }

    tabsWrap.hidden = false;
    quickRow.hidden = false;
    railEl.hidden = false;
    resultsHeaderEl.hidden = false;
    layoutEl.classList.remove("lab-layout--single");

    var items = itemsForStage();
    rebuildRailBounds(items);
    updateTabSummaries(items);

    var visible = sortedVisible(items);
    updateStopPriceLabels(items);

    visibleCountEl.textContent = String(visible.length);
    countLabelEl.textContent = visible.length === 1 ? "flight" : "flights";
    noMatchEl.hidden = visible.length !== 0;

    var kind = flow.stage === "departure" ? "departure" : (flow.stage === "return" ? "return" : "flat");
    listEl.innerHTML = visible.map(function (it) { return cardHtml(kind, it.ref); }).join("");
    wireCardEvents();
  }

  function wireCardEvents() {
    Array.from(listEl.querySelectorAll("[data-lab-choose-departure]")).forEach(function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.dataset.labChooseDeparture;
        flow.chosenGroup = departureGroups.filter(function (g) { return g.key === key; })[0];
        flow.stage = "return";
        activeTab = "best";
        syncTabButtons();
        clearFilterInputs();
        renderStage();
        window.scrollTo({ top: 0, behavior: "auto" });
      });
    });
    Array.from(listEl.querySelectorAll("[data-lab-choose-return]")).forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.dataset.labChooseReturn;
        flow.chosenFlight = flow.chosenGroup.members.filter(function (f) { return f.id === id; })[0];
        flow.stage = "review";
        renderStage();
        window.scrollTo({ top: 0, behavior: "auto" });
      });
    });
  }

  /* ---------------- tabs ---------------- */

  function syncTabButtons() {
    Array.from(root.querySelectorAll("[data-lab-tab]")).forEach(function (b) {
      b.setAttribute("aria-selected", String(b.dataset.labTab === activeTab));
    });
  }
  Array.from(root.querySelectorAll("[data-lab-tab]")).forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.disabled) return;
      activeTab = btn.dataset.labTab;
      syncTabButtons();
      renderStage();
    });
  });

  /* ---------------- quick chips + rail filters ---------------- */

  function syncQuickChips() {
    nonstopQuick.setAttribute("aria-pressed", String(filterState.stops[0] && !filterState.stops[1] && !filterState.stops[2]));
    stop1Quick.setAttribute("aria-pressed", String(filterState.stops[0] && filterState.stops[1] && !filterState.stops[2]));
  }

  nonstopQuick.addEventListener("click", function () {
    var active = nonstopQuick.getAttribute("aria-pressed") === "true";
    filterState.stops[0] = !active;
    filterState.stops[1] = false;
    filterState.stops[2] = false;
    stopInputs[0].checked = filterState.stops[0];
    stopInputs[1].checked = false;
    stopInputs[2].checked = false;
    syncQuickChips();
    renderStage();
  });
  stop1Quick.addEventListener("click", function () {
    var active = stop1Quick.getAttribute("aria-pressed") === "true";
    filterState.stops[0] = !active;
    filterState.stops[1] = !active;
    filterState.stops[2] = false;
    stopInputs[0].checked = filterState.stops[0];
    stopInputs[1].checked = filterState.stops[1];
    stopInputs[2].checked = false;
    syncQuickChips();
    renderStage();
  });
  [0, 1, 2].forEach(function (b) {
    stopInputs[b].addEventListener("change", function () {
      filterState.stops[b] = stopInputs[b].checked;
      syncQuickChips();
      renderStage();
    });
  });
  ["morning", "afternoon", "evening", "night"].forEach(function (b) {
    departInputs[b].addEventListener("change", function () {
      filterState.depart[b] = departInputs[b].checked;
      renderStage();
    });
  });
  durationSlider.addEventListener("input", function () {
    filterState.maxDuration = parseInt(durationSlider.value, 10);
    durationLabel.textContent = "Up to " + hm(filterState.maxDuration);
    renderStage();
  });
  priceSlider.addEventListener("input", function () {
    filterState.maxPrice = parseInt(priceSlider.value, 10);
    priceLabel.textContent = "Up to " + money(filterState.maxPrice);
    renderStage();
  });

  function clearFilterInputs() {
    [0, 1, 2].forEach(function (b) { filterState.stops[b] = false; stopInputs[b].checked = false; });
    ["morning", "afternoon", "evening", "night"].forEach(function (b) { filterState.depart[b] = false; departInputs[b].checked = false; });
    syncQuickChips();
  }

  function clearAllFilters() {
    clearFilterInputs();
    var items = itemsForStage();
    if (items.length) {
      var dMax = Math.max.apply(null, items.map(function (i) { return i.duration; }));
      var pMax = Math.ceil(Math.max.apply(null, items.map(function (i) { return i.price; })));
      filterState.maxDuration = dMax;
      filterState.maxPrice = pMax;
    }
    Object.keys(filterState.airlines).forEach(function (k) { filterState.airlines[k] = false; });
    Object.keys(filterState.layovers).forEach(function (k) { filterState.layovers[k] = false; });
    renderStage();
  }
  document.getElementById("labClearFilters").addEventListener("click", clearAllFilters);
  document.getElementById("labClearFiltersFromEmpty").addEventListener("click", clearAllFilters);

  /* ---------------- mobile filter sheet ---------------- */

  var backdrop = document.getElementById("labSheetBackdrop");
  function openRail(section) {
    railEl.classList.add("is-open");
    backdrop.classList.add("is-open");
    document.body.style.overflow = "hidden";
    if (section && section !== "more") {
      var target = railEl.querySelector('[data-lab-rail-section="' + section + '"]');
      if (target) target.scrollIntoView({ block: "start" });
    }
  }
  function closeRail() {
    railEl.classList.remove("is-open");
    backdrop.classList.remove("is-open");
    document.body.style.overflow = "";
  }
  Array.from(root.querySelectorAll("[data-lab-open-rail]")).forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (window.matchMedia("(max-width: 1024px)").matches) {
        openRail(btn.dataset.labOpenRail);
      } else {
        var section = railEl.querySelector('[data-lab-rail-section="' + btn.dataset.labOpenRail + '"]');
        if (section) section.scrollIntoView({ block: "start", behavior: "smooth" });
      }
    });
  });
  backdrop.addEventListener("click", closeRail);
  document.getElementById("labApplyFilters").addEventListener("click", closeRail);

  /* ---------------- delegated card interactions ---------------- */

  root.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-lab-details-toggle]");
    if (toggle) {
      var card = toggle.closest(".lab-card");
      var details = card.querySelector("[data-lab-details]");
      var expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!expanded));
      details.hidden = expanded;
      if (!expanded) details.scrollIntoView({ block: "nearest" });
      return;
    }
    var why = event.target.closest("[data-lab-why]");
    if (why) {
      var existing = why.parentElement.querySelector(".lab-badge-why-pop");
      if (existing) { existing.remove(); return; }
      document.querySelectorAll(".lab-badge-why-pop").forEach(function (n) { n.remove(); });
      var pop = document.createElement("div");
      pop.className = "lab-badge-why-pop";
      pop.textContent = why.dataset.reason || "";
      why.parentElement.appendChild(pop);
      setTimeout(function () {
        document.addEventListener("click", function onDocClick(e) {
          if (!pop.contains(e.target) && e.target !== why) {
            pop.remove();
            document.removeEventListener("click", onDocClick, true);
          }
        }, true);
      }, 0);
      return;
    }
    var goDeparture = event.target.closest("[data-lab-go-departure]");
    if (goDeparture) {
      flow.stage = "departure";
      flow.chosenGroup = null;
      flow.chosenFlight = null;
      activeTab = "best";
      syncTabButtons();
      clearFilterInputs();
      renderStage();
      window.scrollTo({ top: 0, behavior: "auto" });
      return;
    }
    var goReturn = event.target.closest("[data-lab-go-return]");
    if (goReturn) {
      flow.stage = "return";
      flow.chosenFlight = null;
      activeTab = "best";
      syncTabButtons();
      clearFilterInputs();
      renderStage();
      window.scrollTo({ top: 0, behavior: "auto" });
    }
  });

  /* ---------------- flexible date strip ----------------
     Real calendar dates only — no adjacent-date pricing exists anywhere in
     this app, so no price ever renders on a tile (spec #5: "do not
     fabricate prices"). Picking a different date is still genuinely
     functional: it resubmits the existing /search POST with that date,
     reusing the same backend the classic page already uses. */

  var dateStripEl = document.getElementById("labDateStrip");
  if (dateStripEl && searchForm && searchForm.departDateRaw) {
    (function () {
      var cellsEl = document.getElementById("labDateStripCells");
      var prevBtn = document.getElementById("labDateStripPrev");
      var nextBtn = document.getElementById("labDateStripNext");

      function parseYMD(s) {
        var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
        if (!m) return null;
        return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
      }
      function toYMD(d) {
        return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      }
      function addDays(d, n) {
        var copy = new Date(d.getTime());
        copy.setDate(copy.getDate() + n);
        return copy;
      }
      function startOfToday() {
        var t = new Date();
        t.setHours(0, 0, 0, 0);
        return t;
      }

      var selectedDeparture = parseYMD(searchForm.departDateRaw);
      var selectedReturn = searchForm.returnDateRaw ? parseYMD(searchForm.returnDateRaw) : null;
      if (!selectedDeparture) { dateStripEl.hidden = true; return; }
      var tripLengthDays = selectedReturn ? Math.round((selectedReturn - selectedDeparture) / 86400000) : null;

      var windowStart = addDays(selectedDeparture, -2);
      var todayFloor = startOfToday();

      function submitSearchFor(departDate) {
        var form = document.createElement("form");
        form.method = "post";
        form.action = searchForm.searchUrl;
        form.style.display = "none";
        function hidden(name, value) {
          var input = document.createElement("input");
          input.type = "hidden";
          input.name = name;
          input.value = value == null ? "" : String(value);
          form.appendChild(input);
        }
        hidden("mode", "standard");
        hidden("force_refresh", "1");
        hidden("trip_type", searchForm.tripTypeRaw);
        hidden("origin", searchForm.originRaw);
        hidden("destination", searchForm.destinationRaw);
        hidden("passengers", searchForm.passengersRaw);
        hidden("cabin", searchForm.cabinRaw);
        if (searchForm.nonstopRaw) hidden("nonstop", "on");
        hidden("depart_date", toYMD(departDate));
        if (tripLengthDays !== null) hidden("return_date", toYMD(addDays(departDate, tripLengthDays)));
        document.body.appendChild(form);
        form.submit();
      }

      function renderCells() {
        var frag = document.createDocumentFragment();
        for (var i = 0; i < 5; i++) {
          var cellDate = addDays(windowStart, i);
          var isSelected = toYMD(cellDate) === toYMD(selectedDeparture);
          var isPast = cellDate < todayFloor;
          var weekday = new Intl.DateTimeFormat("en-US", { weekday: "short" }).format(cellDate);
          var monthDay = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(cellDate);
          var cell = document.createElement(isSelected || isPast ? "div" : "button");
          if (!isSelected && !isPast) cell.type = "button";
          cell.className = "lab-datestrip-cell" + (isSelected ? " is-selected" : "") + (isPast ? " is-past" : "");
          cell.innerHTML =
            '<span class="lab-datestrip-day">' + esc(weekday) + "</span>" +
            '<span class="lab-datestrip-date">' + esc(monthDay) + "</span>" +
            (isSelected ? '<span class="lab-datestrip-tag">Selected</span>' : "");
          if (!isSelected && !isPast) {
            cell.addEventListener("click", function () {
              submitSearchFor(new Date(this._cellDate.getTime()));
            });
            cell._cellDate = cellDate;
          }
          frag.appendChild(cell);
        }
        cellsEl.innerHTML = "";
        cellsEl.appendChild(frag);
      }

      prevBtn.addEventListener("click", function () {
        windowStart = addDays(windowStart, -1);
        renderCells();
      });
      nextBtn.addEventListener("click", function () {
        windowStart = addDays(windowStart, 1);
        renderCells();
      });

      renderCells();
    })();
  }

  /* ---------------- price intelligence ----------------
     No forecast/history backend exists for this app, so this never claims
     a price judgment ("Good price", "prices may rise", etc.) — only the
     honest "Track price" affordance the spec explicitly allows as the
     no-data fallback. Purely a client-side toggle; nothing is persisted,
     so it never implies a notification that won't actually arrive. */
  var trackBtn = document.getElementById("labTrackPrice");
  if (trackBtn) {
    trackBtn.addEventListener("click", function () {
      var tracking = trackBtn.getAttribute("aria-pressed") === "true";
      trackBtn.setAttribute("aria-pressed", String(!tracking));
      trackBtn.textContent = tracking ? "Track price" : "Tracking";
    });
  }

  /* ---------------- edit search ---------------- */

  var editBtn = document.getElementById("labEditSearch");
  if (editBtn) {
    editBtn.addEventListener("click", function () {
      document.body.classList.remove("lab-active");
      try {
        document.cookie = "resultsView=classic;path=/;max-age=" + 60 * 60 * 24 * 180 + ";SameSite=Lax";
      } catch (e) {}
      var labToggle = document.getElementById("labViewToggle");
      if (labToggle) labToggle.setAttribute("aria-pressed", "false");
      window.scrollTo({ top: 0, behavior: "auto" });
      var routeSeg = document.querySelector('[data-srb-seg="route"]');
      if (routeSeg) setTimeout(function () { routeSeg.click(); }, 50);
    });
  }

  /* ---------------- initial paint ---------------- */

  renderStage();
})();
