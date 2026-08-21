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


  /* ---------------- DOM refs ---------------- */

  var listEl = document.getElementById("labList");
  var progressEl = document.getElementById("labProgress");
  var pinnedEl = document.getElementById("labPinnedDeparture");
  var visibleCountEl = document.getElementById("labVisibleCount");
  var countLabelEl = document.getElementById("labResultsCountLabel");
  var noMatchEl = document.getElementById("labNoMatch");
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
          departIso: g.out.departIso, arriveIso: g.out.arriveIso, airlineName: g.airlineName, layovers: g.out.layovers, ref: g,
        };
      });
    }
    if (flow.stage === "return" && flow.chosenGroup) {
      return flow.chosenGroup.members.map(function (f) {
        return {
          key: f.id, price: f.price, duration: f.ret.durationMin, stops: f.ret.stops,
          departIso: f.ret.departIso, arriveIso: f.ret.arriveIso, airlineName: f.airlineName, layovers: f.ret.layovers, ref: f,
        };
      });
    }
    if (flow.stage === "flat") {
      return allFlights.map(function (f) {
        return {
          key: f.id, price: f.price, duration: f.totalDurationMin, stops: f.totalStops,
          departIso: f.out ? f.out.departIso : "", arriveIso: f.out ? f.out.arriveIso : "", airlineName: f.airlineName,
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
  /* Calendar-day delta between two ISO timestamps, comparing date parts
     only so it's unaffected by whether the strings carry a timezone
     suffix — both already represent each airport's local wall-clock time,
     the same convention departTime/arriveTime already rely on. Real
     arithmetic over real fields already in the payload, never fabricated. */
  function dayOffset(departIso, arriveIso) {
    var d = /^(\d{4})-(\d{2})-(\d{2})/.exec(departIso || "");
    var a = /^(\d{4})-(\d{2})-(\d{2})/.exec(arriveIso || "");
    if (!d || !a) return 0;
    var dUTC = Date.UTC(+d[1], +d[2] - 1, +d[3]);
    var aUTC = Date.UTC(+a[1], +a[2] - 1, +a[3]);
    return Math.round((aUTC - dUTC) / 86400000);
  }

  function legRow(leg, showLabel) {
    var stopsClass = leg.stops === 0 ? "is-nonstop" : (leg.stops >= 2 ? "is-warn" : "");
    var mid = leg.stops === 1 && leg.layovers.length
      ? leg.stopsLabel + " &middot; " + esc(leg.layovers[0].code)
      : leg.stopsLabel;
    var offset = dayOffset(leg.departIso, leg.arriveIso);
    var offsetHtml = offset > 0 ? '<span class="lab-leg-offset">+' + offset + "</span>" : "";
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
          '<span class="lab-leg-time">' + esc(leg.arriveTime) + offsetHtml + "</span>" +
          '<span class="lab-leg-iata">' + esc(leg.destCode) + '<span class="lab-leg-day">' + esc(leg.arriveDay) + "</span></span>" +
        "</div>" +
      "</div>"
    );
  }

  var ICON_LUGGAGE = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="7" width="16" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/><line x1="9" y1="11" x2="9" y2="16"/><line x1="15" y1="11" x2="15" y2="16"/></svg>';
  var ICON_WARN = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 9v4"/><path d="M10.6 3.9 2.4 18a1.6 1.6 0 0 0 1.4 2.4h16.4a1.6 1.6 0 0 0 1.4-2.4L13.4 3.9a1.6 1.6 0 0 0-2.8 0Z"/><path d="M12 17h.01"/></svg>';

  /* Stop status has exactly one home: the journey line itself (legRow's mid
     lane). This row is baggage + material-risk warnings only — no repeated
     "Nonstop"/"N stops" (spec #17). Unknown baggage is never a green check
     (this app has no fare-bundle baggage data at itinerary level). No
     emoji in production UI (spec #31) — vector icons only. */
  function journeyMeta(warnings) {
    var bits = ['<span class="lab-meta-item lab-meta-item--muted">' + ICON_LUGGAGE + " Baggage details at fare selection</span>"];
    (warnings || []).forEach(function (w) {
      bits.push('<span class="lab-meta-item lab-meta-item--warn">' + ICON_WARN + " " + esc(w) + "</span>");
    });
    return '<div class="lab-journey-meta">' + bits.join("") + "</div>";
  }

  /* ---------------- trade-off math (spec #29) ----------------
     Every non-top card is compared against the stage's Skairova Best item
     (the server-recommended items[0] for this stage — see bestRefForStage
     below), so the user never has to do the subtraction themselves. Purely
     arithmetic over real payload numbers; nothing here invents a claim. */
  function computeTradeoff(item, best) {
    if (!best || item.key === best.key) return null;
    var priceDelta = item.price - best.price;
    var durDelta = item.duration - best.duration;
    var stopsDelta = item.stops - best.stops;
    var depDeltaMin = null;
    if (item.departIso && best.departIso) {
      var a = new Date(item.departIso).getTime();
      var b = new Date(best.departIso).getTime();
      if (!isNaN(a) && !isNaN(b)) depDeltaMin = Math.round((a - b) / 60000);
    }
    return { priceDelta: priceDelta, durDelta: durDelta, stopsDelta: stopsDelta, depDeltaMin: depDeltaMin };
  }

  function tradeoffFragment(tradeoff) {
    if (!tradeoff) return null;
    var clause = null;
    if (tradeoff.stopsDelta > 0) {
      clause = "adds " + tradeoff.stopsDelta + " stop" + (tradeoff.stopsDelta > 1 ? "s" : "");
    } else if (tradeoff.stopsDelta < 0) {
      clause = "fewer stops";
    } else if (tradeoff.depDeltaMin !== null && Math.abs(tradeoff.depDeltaMin) >= 30) {
      clause = "leaves " + hm(Math.abs(tradeoff.depDeltaMin)) + (tradeoff.depDeltaMin > 0 ? " later" : " earlier");
    } else if (Math.abs(tradeoff.durDelta) >= 20) {
      clause = (tradeoff.durDelta > 0 ? "+" : "-") + hm(Math.abs(tradeoff.durDelta)) + " total";
    }
    if (!clause) return null;
    return {
      isSavings: false,
      html: "<strong>" + esc(clause) + "</strong>",
    };
  }

  /* ---------------- badges (spec #27, #28) ----------------
     The item that IS the stage's Skairova Best (server-recommended items[0])
     always gets the AI badge, regardless of which raw label the backend
     assigned it. Every other badge gets a contextual label computed from
     the real trade-off rather than the generic "Alternative option". */
  function contextualBadge(rawBadge, item, tradeoff) {
    if (rawBadge === "Top pick") return { text: "Skairova Best", cls: "match", sparkle: true };
    if (rawBadge === "Lowest price" || rawBadge === "Cheapest") return { text: "Cheapest", cls: "positive" };
    if (rawBadge === "Fastest" || rawBadge === "Fastest trip") return { text: "Fastest", cls: "neutral" };
    if (rawBadge === "Most direct") return { text: "Fewest stops", cls: "neutral" };
    if (rawBadge === "Earliest departure") return { text: "Earliest departure", cls: "neutral" };
    if (rawBadge === "Arrives earliest") return { text: "Arrives earliest", cls: "neutral" };
    if (rawBadge === "Alternative option") {
      if (tradeoff && tradeoff.priceDelta < -0.5) return { text: "Cheapest alternative", cls: "positive" };
      if (tradeoff && tradeoff.stopsDelta < 0) return { text: "Fewer stops", cls: "neutral" };
      var bucket = timeBucket(hourOf(item.departIso));
      if (bucket) return { text: "Best " + bucket.charAt(0).toUpperCase() + bucket.slice(1) + " flight", cls: "neutral" };
      return { text: "Alternative option", cls: "neutral" };
    }
    return rawBadge ? { text: rawBadge, cls: "neutral" } : null;
  }

  function whyButton(reason, ariaLabel) {
    if (!reason) return "";
    return '<button type="button" class="lab-badge-why" data-lab-why data-reason="' + esc(reason) + '" aria-label="' + esc(ariaLabel) + '"><svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4.5M12 8h.01" stroke-linecap="round"/></svg></button>';
  }

  /* Non-top cards get one quiet plain-text line (label + trade-off merged
     with a middle dot, e.g. "Best afternoon · +$1") instead of a colored
     pill — a second visual badge next to Skairova Best's own was reading
     as competing emphasis. The one exception is a raw "Top pick" badge
     reaching a non-top card (the committed review-stage card), which keeps
     the same plain Skairova Best treatment for consistency. */
  function metaRow(isTop, rawBadge, reasoning, item, tradeoff) {
    var disp = isTop
      ? { text: "Skairova Best", cls: "match" }
      : (rawBadge ? contextualBadge(rawBadge, item, tradeoff) : null);
    if (disp && disp.cls === "match") {
      return (
        '<div class="lab-card-badges"><span class="lab-badge lab-badge--match"><span class="lab-spark" aria-hidden="true">✦</span> ' +
          esc(disp.text) + whyButton(reasoning, "Why this is ranked here") +
        "</span></div>"
      );
    }
    var tf = tradeoffFragment(tradeoff);
    if (!disp && !tf) return "";
    var cls = disp ? disp.cls : (tf.isSavings ? "positive" : "neutral");
    var parts = [];
    if (disp) parts.push(esc(disp.text));
    if (tf) parts.push(tf.html);
    return (
      '<div class="lab-meta-line lab-meta-line--' + cls + '">' +
        parts.join(' <span class="lab-meta-dot" aria-hidden="true">&middot;</span> ') +
        whyButton(reasoning, "Why this is ranked here") +
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

  /* ---------------- top-card reason line (spatial pass, spec #27) ----------------
     Restored: the density pass had dropped this to save vertical space,
     but the spatial-composition spec explicitly wants it back with its own
     breathing room now that the Skairova Best card has a taller allowance
     (130-155px) than a normal card. Built from the item's own real
     price/stops/duration — never invented — same as before. */
  function bestSummaryLine(item) {
    if (!item) return "";
    var stopsPhrase = item.stops === 0 ? "nonstop service" : (item.stops + " stop" + (item.stops > 1 ? "s" : ""));
    return "Best balance of fare, " + stopsPhrase + " and journey time.";
  }

  /* "round trip · per traveler" / "one way · total for 3 travelers" — the
     one qualifier line every card uses under the price (spec #20: never
     the words "Total trip" as a label). */
  function tripQualifier(pax) {
    var tripWord = isRoundTrip ? "round trip" : "one way";
    var paxWord = pax > 1 ? "total for " + pax + " travelers" : "per traveler";
    return tripWord + " · " + paxWord;
  }

  /* Renders one card. `kind` = "departure" | "return" | "review" | "flat".
     `stageItem`/`bestRef` are the normalized {key,price,duration,stops,...}
     views from itemsForStage() — present for every kind except "review" —
     used only to compute isTop/tradeoff; never re-rendered directly. */
  function cardHtml(kind, entity, stageItem, bestRef) {
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
      ctaHtml = '<button type="button" class="lab-select-btn" data-lab-choose-departure="' + esc(g.key) + '">Select flight</button>';
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
      ctaHtml = '<button type="button" class="lab-select-btn" data-lab-choose-return="' + esc(f.id) + '">Select flight</button>';
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
      var ctaClass = kind === "review" ? "lab-select-btn lab-select-btn--final" : "lab-select-btn";
      var ctaLabel = kind === "review" ? "Book flight" : "Select flight";
      ctaHtml = fl.checkoutUrl
        ? '<a class="' + ctaClass + '" href="' + esc(fl.checkoutUrl) + '"' + (fl.tripCombo ? "" : ' target="_blank" rel="noopener noreferrer"') + ' data-result-id="' + esc(fl.id) + '">' + ctaLabel + '</a>'
        : '<span class="' + ctaClass + '" aria-disabled="true">Unavailable</span>';
    }

    var showLegLabel = legs.length > 1;
    var isTop = !!(stageItem && bestRef && stageItem.key === bestRef.key);
    var tradeoff = (!isTop && stageItem && bestRef) ? computeTradeoff(stageItem, bestRef) : null;

    /* Expedia-pattern rebuild: one horizontal band — airline column,
       journey column, price/CTA column — instead of stacked full-width
       rows. Any contextual badge/tradeoff sits in its own thin strip above
       the band (where a "Best value" ribbon would sit), not squeezed into
       the airline row. */
    var cardMeta = metaRow(isTop, badge, reasoning, stageItem || {}, tradeoff);
    var summaryHtml = isTop ? '<span class="lab-card-summary">' + esc(bestSummaryLine(stageItem)) + "</span>" : "";
    var tagRow = (cardMeta || summaryHtml) ? '<div class="lab-card-tag-row">' + cardMeta + summaryHtml + "</div>" : "";

    var priceNotes = ['<span class="lab-price-note">' + esc(priceQualifier) + "</span>"];
    if (secondaryNote) priceNotes.push('<span class="lab-price-note">' + esc(secondaryNote) + "</span>");
    priceNotes.push('<span class="lab-price-note lab-price-note--taxes">Taxes &amp; fees included</span>');
    if (cueHtml) priceNotes.push(cueHtml);

    return (
      '<article class="lab-card' + (isTop ? " lab-card--top" : "") + '" data-key="' + esc(key) + '">' +
        tagRow +
        '<div class="lab-card-main">' +
          carrierBlock(airlineName, logoUrl, mix) +
          '<div class="lab-card-journey">' +
            legs.map(function (l) { return legRow(l, showLegLabel); }).join("") +
            journeyMeta([]) +
          "</div>" +
          '<div class="lab-card-price">' +
            '<div class="lab-pricing-info">' +
              '<span class="lab-price-amt">' + priceAmt + "</span>" +
              '<div class="lab-price-notes">' + priceNotes.join("") + "</div>" +
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
    if (filterState.minPrice && item.price < filterState.minPrice) return false;
    if (!passesNlHourFilter(item)) return false;
    if (anyChecked(filterState.airlines) && !airlineTokens(item.airlineName).some(function (n) { return filterState.airlines[n]; })) return false;
    if (anyChecked(filterState.layovers) && item.layovers.length && !item.layovers.some(function (l) { return filterState.layovers[l.code]; })) return false;
    if (anyChecked(filterState.layovers) && !item.layovers.length && item.stops > 0) return false;
    return true;
  }

  var activeTab = "best";

  /* Classic three-way split (spec: Expedia's Best/Cheapest/Quickest tabs).
     Stops/departure-time/etc. live only as rail filters — nothing here
     sorts by them; every mode is purely an ordering over the already-
     filtered set. */
  function sortedVisible(items) {
    var visible = items.filter(passesFilters);
    if (activeTab === "cheapest") visible.sort(function (a, b) { return a.price - b.price || a.duration - b.duration; });
    else if (activeTab === "fastest") visible.sort(function (a, b) { return a.duration - b.duration || a.price - b.price; });
    // "best" keeps original (server-recommended) order — items arrays are
    // already built in that order for every stage.
    return visible;
  }

  /* ---------------- render orchestration ---------------- */

  /* Recomputes slider ranges + the airline/layover checklists for the
     current stage's full item set. Bounds are always refreshed, but an
     in-progress user constraint (slider dragged down, an airline checked,
     the AI filter's price ceiling) must survive the next render — this
     used to unconditionally snap every value back to "unrestricted" on
     every renderStage() call, silently no-op'ing the price/duration/
     airline/layover filters (and the AI filter's price parsing) the
     instant they were set. Stage transitions still fully reset via
     clearFilterInputs(), which runs on every departure/return/review
     transition. */
  function rebuildRailBounds(items) {
    if (!items.length) return;
    var durations = items.map(function (i) { return i.duration; });
    var prices = items.map(function (i) { return i.price; });
    var dMax = Math.max.apply(null, durations);
    var pMax = Math.max.apply(null, prices);
    var pMin = Math.min.apply(null, prices);
    durationSlider.min = "0";
    durationSlider.max = String(Math.max(dMax, 60));
    if (!(filterState.maxDuration <= dMax)) filterState.maxDuration = dMax;
    durationSlider.value = String(filterState.maxDuration);
    durationLabel.textContent = "Up to " + hm(filterState.maxDuration);

    priceSlider.min = String(Math.floor(pMin));
    priceSlider.max = String(Math.ceil(pMax));
    if (!(filterState.maxPrice <= Math.ceil(pMax))) filterState.maxPrice = Math.ceil(pMax);
    priceSlider.value = String(filterState.maxPrice);
    priceLabel.textContent = "Up to " + money(filterState.maxPrice);

    var airlineNames = Array.from(new Set(items.reduce(function (acc, i) { return acc.concat(airlineTokens(i.airlineName)); }, []))).sort();
    var prevAirlines = filterState.airlines || {};
    filterState.airlines = {};
    airlineListEl.innerHTML = "";
    airlineNames.forEach(function (name) {
      filterState.airlines[name] = !!prevAirlines[name];
      var label = document.createElement("label");
      label.className = "lab-check";
      label.innerHTML = '<input type="checkbox" data-lab-filter-airline="' + esc(name) + '" /> <span></span>';
      label.querySelector("span").textContent = name;
      label.querySelector("input").checked = filterState.airlines[name];
      airlineListEl.appendChild(label);
    });
    Array.from(airlineListEl.querySelectorAll("[data-lab-filter-airline]")).forEach(function (input) {
      input.addEventListener("change", function () {
        filterState.airlines[input.dataset.labFilterAirline] = input.checked;
        renderStage();
      });
    });

    var layoverCodes = Array.from(new Set(items.reduce(function (acc, i) { return acc.concat(i.layovers.map(function (l) { return l.code; })); }, []))).sort();
    var prevLayovers = filterState.layovers || {};
    filterState.layovers = {};
    layoverListEl.innerHTML = "";
    var layoverGroup = layoverListEl.closest(".lab-rail-group");
    layoverGroup.hidden = layoverCodes.length === 0;
    layoverCodes.forEach(function (code) {
      filterState.layovers[code] = !!prevLayovers[code];
      var label = document.createElement("label");
      label.className = "lab-check";
      label.innerHTML = '<input type="checkbox" data-lab-filter-layover="' + esc(code) + '" /> <span></span>';
      label.querySelector("span").textContent = code.toUpperCase();
      label.querySelector("input").checked = filterState.layovers[code];
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
      railEl.hidden = true;
      resultsHeaderEl.hidden = true;
      layoutEl.classList.add("lab-layout--single");
      noMatchEl.hidden = true;
      listEl.innerHTML = cardHtml("review", flow.chosenFlight) +
        '<div class="lab-review-actions"><button type="button" class="lab-clear-filters" data-lab-go-return>← Change return</button></div>';
      wireCardEvents();
      return;
    }

    railEl.hidden = false;
    resultsHeaderEl.hidden = false;
    layoutEl.classList.remove("lab-layout--single");

    var items = itemsForStage();
    var bestRef = items.length ? items[0] : null;
    rebuildRailBounds(items);
    updateTabSummaries(items);

    var visible = sortedVisible(items);
    updateStopPriceLabels(items);

    visibleCountEl.textContent = String(visible.length);
    countLabelEl.textContent = visible.length === 1 ? "flight" : "flights";
    noMatchEl.hidden = visible.length !== 0;

    var kind = flow.stage === "departure" ? "departure" : (flow.stage === "return" ? "return" : "flat");
    listEl.innerHTML = visible.map(function (it) { return cardHtml(kind, it.ref, it, bestRef); }).join("");
    wireCardEvents();
  }

  function wireCardEvents() {
    Array.from(listEl.querySelectorAll("[data-lab-choose-departure]")).forEach(function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.dataset.labChooseDeparture;
        flow.chosenGroup = departureGroups.filter(function (g) { return g.key === key; })[0];
        flow.stage = "return";
        activeTab = "best";
        syncSortControl();
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

  /* ---------------- sort tabs (Best / Cheapest / Quickest) ---------------- */
  /* Each tab shows the price+duration of the item that mode would surface
     first — real numbers from the current stage's own item set, computed
     fresh on every render, never a static label. */

  var tabValueEls = {
    best: document.getElementById("labTabValueBest"),
    cheapest: document.getElementById("labTabValueCheapest"),
    fastest: document.getElementById("labTabValueFastest"),
  };

  function updateTabSummaries(items) {
    if (!items.length) return;
    var byPrice = items.slice().sort(function (a, b) { return a.price - b.price; });
    var byDuration = items.slice().sort(function (a, b) { return a.duration - b.duration; });
    var summaries = {
      best: items[0],
      cheapest: byPrice[0],
      fastest: byDuration[0],
    };
    Object.keys(summaries).forEach(function (mode) {
      var el = tabValueEls[mode];
      if (!el) return;
      var it = summaries[mode];
      el.textContent = money(it.price) + " · " + hm(it.duration);
    });
  }

  function syncSortControl() {
    Array.from(root.querySelectorAll(".lab-sort-tab")).forEach(function (tab) {
      tab.setAttribute("aria-selected", String(tab.dataset.labTab === activeTab));
    });
  }
  Array.from(root.querySelectorAll(".lab-sort-tab")).forEach(function (tab) {
    tab.addEventListener("click", function () {
      activeTab = tab.dataset.labTab;
      syncSortControl();
      renderStage();
    });
  });

  /* ---------------- quick chips + rail filters ---------------- */

  function syncQuickChips() {
    if (nonstopQuick) {
      nonstopQuick.setAttribute("aria-pressed", String(filterState.stops[0] && !filterState.stops[1] && !filterState.stops[2]));
    }
    if (stop1Quick) {
      stop1Quick.setAttribute("aria-pressed", String(filterState.stops[0] && filterState.stops[1] && !filterState.stops[2]));
    }
  }

  if (nonstopQuick) {
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
  }
  if (stop1Quick) {
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
  }
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

  /* Full filter reset — called on every departure/return/review stage
     transition, since a price cap or airline pick from one leg's item set
     is meaningless (or actively hides everything) against the next leg's
     different price range and carrier mix. */
  function clearFilterInputs() {
    [0, 1, 2].forEach(function (b) { filterState.stops[b] = false; stopInputs[b].checked = false; });
    ["morning", "afternoon", "evening", "night"].forEach(function (b) { filterState.depart[b] = false; departInputs[b].checked = false; });
    filterState.maxDuration = Infinity;
    filterState.maxPrice = Infinity;
    filterState.minPrice = 0;
    filterState.departAfterHour = null;
    filterState.departBeforeHour = null;
    Object.keys(filterState.airlines).forEach(function (k) { filterState.airlines[k] = false; });
    Object.keys(filterState.layovers).forEach(function (k) { filterState.layovers[k] = false; });
    appliedNlFilters = [];
    if (aiFilterInput) aiFilterInput.value = "";
    renderAppliedNlChips();
    syncQuickChips();
  }

  function clearAllFilters() {
    clearFilterInputs();
    renderStage();
  }
  document.getElementById("labClearFilters").addEventListener("click", clearAllFilters);
  document.getElementById("labClearFiltersFromEmpty").addEventListener("click", clearAllFilters);

  /* ---------------- mobile filter sheet ---------------- */

  var backdrop = document.getElementById("labSheetBackdrop");
  function openRail(section) {
    railEl.classList.add("is-open");
    backdrop.classList.add("is-open");
    document.body.classList.add("lab-filter-open");
    document.body.style.overflow = "hidden";
    if (section && section !== "more") {
      var target = railEl.querySelector('[data-lab-rail-section="' + section + '"]');
      if (target) target.scrollIntoView({ block: "start" });
    }
  }
  function closeRail() {
    railEl.classList.remove("is-open");
    backdrop.classList.remove("is-open");
    document.body.classList.remove("lab-filter-open");
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
      /* Anchor to the badge/meta-line row, not the tiny inline icon itself
         — the icon's x-position drifts with how long the label text is
         (e.g. "Skairova Best" vs "Best Afternoon flight · +$7"), which
         made a fixed left/right CSS anchor overflow the viewport for some
         label lengths and not others. The row is a stable anchor at every
         width: compact and right-aligned on desktop, full card width on
         mobile — either way the popover stays within it. */
      var anchor = why.closest(".lab-card-badges, .lab-meta-line") || why;
      var existingPop = document.querySelector(".lab-why-pop");
      var sameTrigger = existingPop && existingPop.parentElement === anchor;
      if (existingPop) existingPop.remove();
      if (sameTrigger) return;
      var reasonText = why.dataset.reason || "";
      if (!reasonText) return;
      /* Real backend copy (e.g. "the lowest fare in this search; nonstop,
         as requested") re-formatted as a checklist — never new claims. */
      var reasonItems = reasonText.split(/;\s*/).map(function (s) { return s.trim(); }).filter(Boolean).map(function (s) { return s.charAt(0).toUpperCase() + s.slice(1); });
      var isAi = !!why.closest(".lab-badge--match");
      var pop = document.createElement("div");
      pop.className = "lab-why-pop";
      pop.innerHTML =
        (isAi ? '<div class="lab-why-pop-title">Why Skairova recommends this</div>' : "") +
        "<ul>" + reasonItems.map(function (s) {
          return '<li><svg viewBox="0 0 20 20" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="4 10 8 14 16 5"/></svg><span>' + esc(s) + "</span></li>";
        }).join("") + "</ul>";
      if (getComputedStyle(anchor).position === "static") anchor.style.position = "relative";
      anchor.appendChild(pop);
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
      syncSortControl();
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
      syncSortControl();
      clearFilterInputs();
      renderStage();
      window.scrollTo({ top: 0, behavior: "auto" });
    }
  });

  /* ---------------- flexible date strip ----------------
     Each date shows the lowest live fare for that exact date and trip
     length. The selected date can use the flights already on the page;
     nearby dates are fetched through the same cached offer lookup used by
     the rest of the app. Nothing is invented when a lookup has no result. */

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
      var datePrices = Object.create(null);
      var selectedKey = toYMD(selectedDeparture);
      var selectedLowest = allFlights.reduce(function (lowest, flight) {
        var price = Number(flight && flight.price);
        return price > 0 && (lowest === null || price < lowest) ? price : lowest;
      }, null);
      if (selectedLowest !== null) datePrices[selectedKey] = { price: selectedLowest };

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

      function visibleDateKeys() {
        var keys = [];
        for (var i = 0; i < 5; i++) {
          var cellDate = addDays(windowStart, i);
          if (cellDate >= todayFloor) keys.push(toYMD(cellDate));
        }
        return keys;
      }

      function loadVisiblePrices() {
        if (!searchForm.datePriceUrl) return;
        var keys = visibleDateKeys().filter(function (key) { return !datePrices[key]; });
        if (!keys.length) return;
        keys.forEach(function (key) { datePrices[key] = { loading: true }; });
        renderCells();
        fetch(searchForm.datePriceUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            dates: keys,
            origin: searchForm.originRaw,
            destination: searchForm.destinationRaw,
            tripType: searchForm.tripTypeRaw,
            passengers: searchForm.passengersRaw,
            cabin: searchForm.cabinRaw,
            nonstop: Boolean(searchForm.nonstopRaw),
            tripLengthDays: tripLengthDays
          })
        }).then(function (response) {
          if (!response.ok) throw new Error("Could not load fares");
          return response.json();
        }).then(function (result) {
          var prices = result && result.prices ? result.prices : {};
          keys.forEach(function (key) {
            var value = prices[key];
            datePrices[key] = value && Number(value.price) > 0
              ? { price: Number(value.price), currency: value.currency || "USD" }
              : { unavailable: true };
          });
          renderCells();
        }).catch(function () {
          keys.forEach(function (key) { datePrices[key] = { unavailable: true }; });
          renderCells();
        });
      }

      function priceMarkup(cellDate, isPast) {
        if (isPast) return '<span class="lab-datestrip-price" aria-hidden="true"></span>';
        var priceInfo = datePrices[toYMD(cellDate)];
        if (priceInfo && Number(priceInfo.price) > 0) {
          return '<span class="lab-datestrip-price" aria-label="Lowest available fare ' + esc(money(priceInfo.price)) + '">from ' + esc(money(priceInfo.price)) + "</span>";
        }
        if (priceInfo && priceInfo.unavailable) {
          return '<span class="lab-datestrip-price is-unavailable">Unavailable</span>';
        }
        return '<span class="lab-datestrip-price is-loading">Checking…</span>';
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
            priceMarkup(cellDate, isPast);
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
        loadVisiblePrices();
      });
      nextBtn.addEventListener("click", function () {
        windowStart = addDays(windowStart, 1);
        renderCells();
        loadVisiblePrices();
      });

      renderCells();
      loadVisiblePrices();
    })();
  }

  /* ---------------- ranking explainer popover (spec #12, #13) ----------------
     Small "i" sitting beside the sort trigger inside .lab-sort — its own
     handler must stop propagation or a click would also bubble into the
     sort-menu's outside-click listener and immediately close whichever of
     the two just opened. */
  var howRankingBtn = root.querySelector("[data-lab-how-ranking]");
  var howRankingPop = document.getElementById("labHowRankingPop");
  if (howRankingBtn && howRankingPop) {
    function toggleHowRanking(e) {
      e.stopPropagation();
      e.preventDefault();
      howRankingPop.hidden = !howRankingPop.hidden;
    }
    howRankingBtn.addEventListener("click", toggleHowRanking);
    howRankingBtn.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") toggleHowRanking(e);
    });
    document.addEventListener("click", function (e) {
      if (!howRankingPop.hidden && !howRankingPop.contains(e.target) && e.target !== howRankingBtn) {
        howRankingPop.hidden = true;
      }
    });
  }

  /* ---------------- AI natural-language filter (spec #14) ----------------
     Client-side heuristic parsing over the free-text box, in the same
     regex-driven style this app already uses for its AI search bar
     (parse_ai_flight_request in app.py) — no backend call, no external LLM.
     Only acts on things the user explicitly stated: nonstop/stop count,
     a price ceiling, an explicit "after/before" hour, an "early morning"
     exclusion, or an airline name that actually appears in this result
     set. Every constraint it derives reuses the exact same filterState the
     checkboxes already drive, and renders as a removable chip so nothing
     is applied invisibly. */
  filterState.departAfterHour = null;
  filterState.departBeforeHour = null;
  filterState.minPrice = 0;

  var aiFilterInput = document.getElementById("labAiFilterInput");
  var aiFilterGo = document.getElementById("labAiFilterGo");
  var aiFilterApplied = document.getElementById("labAiFilterApplied");
  var appliedNlFilters = []; // [{ key, label }]

  function hourFrom(hStr, ampm) {
    var h = parseInt(hStr, 10) % 12;
    if (/pm/i.test(ampm)) h += 12;
    return h;
  }

  function parseAiFilterText(text) {
    var q = " " + text.toLowerCase().trim() + " ";
    var applied = [];
    function add(key, label) {
      if (!applied.some(function (filter) { return filter.key === key; })) applied.push({ key: key, label: label });
    }

    if (/\b(nonstop|non-stop|no stops|no layovers?|direct(?:\s+flight)?s?|direct only|only direct)\b/.test(q)) {
      filterState.stops[0] = true; filterState.stops[1] = false; filterState.stops[2] = false;
      add("stops", "Nonstop");
    } else {
      var stopMax = /\b(?:up to |at most |no more than |maximum of )?(\d)\s*stop[s]?(?:\s*(?:or fewer|or less|max|maximum))?\b/.exec(q);
      if (stopMax) {
        var n = parseInt(stopMax[1], 10);
        filterState.stops[0] = true;
        if (n >= 1) filterState.stops[1] = true;
        if (n >= 2) filterState.stops[2] = true;
        add("stops", n + " stop" + (n === 1 ? "" : "s") + " or fewer");
      }
    }

    var range = /\b(?:between|from)\s*\$?(\d[\d,]*)\s*(?:and|to|-)\s*\$?(\d[\d,]*)/.exec(q);
    if (range) {
      var low = parseInt(range[1].replace(/,/g, ""), 10);
      var high = parseInt(range[2].replace(/,/g, ""), 10);
      filterState.minPrice = Math.min(low, high);
      filterState.maxPrice = Math.max(low, high);
      add("minPrice", "From " + money(filterState.minPrice));
      add("maxPrice", "Up to " + money(filterState.maxPrice));
    }
    /* The final look-ahead keeps phrases such as "max 1 stop" or
       "under 6 hours" from accidentally being treated as a dollar cap. */
    var under = /\b(?:under|below|less than|cheaper than|up to|budget(?:\s+of)?|maximum|max)\s*(?:price|fare|budget)?\s*\$?(\d[\d,]*)(?!\s*(?:stops?|hours?|hrs?|minutes?|mins?))/.exec(q);
    if (under) {
      var maxP = parseInt(under[1].replace(/,/g, ""), 10);
      filterState.maxPrice = maxP;
      if (priceSlider) priceSlider.value = String(Math.min(maxP, parseInt(priceSlider.max || maxP, 10)));
      if (priceLabel) priceLabel.textContent = "Up to " + money(maxP);
      add("maxPrice", "Up to " + money(maxP));
    }
    var over = /\b(?:over|above|more than|at least|minimum|min)\s*(?:price|fare|budget)?\s*\$?(\d[\d,]*)(?!\s*(?:stops?|hours?|hrs?|minutes?|mins?))/.exec(q);
    if (over) {
      var minP = parseInt(over[1].replace(/,/g, ""), 10);
      filterState.minPrice = minP;
      add("minPrice", "From " + money(minP));
    }

    var duration = /\b(?:under|below|less than|shorter than|within|no more than|at most|max(?:imum)?)\s*(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|hr|h)\b/.exec(q);
    if (duration) {
      var maxDuration = Math.round(parseFloat(duration[1]) * 60);
      filterState.maxDuration = maxDuration;
      add("maxDuration", "Up to " + hm(maxDuration));
    }

    var after = /\bafter\s*(\d{1,2})(?::\d{2})?\s*(am|pm)?/.exec(q);
    if (after) {
      var h = after[2] ? hourFrom(after[1], after[2]) : parseInt(after[1], 10);
      filterState.departAfterHour = h;
      add("departAfterHour", "After " + esc(after[1]) + (after[2] ? " " + after[2].toUpperCase() : ""));
    } else if (/\b(no early morning|not early morning|skip early morning)\b/.test(q)) {
      filterState.departAfterHour = 8;
      add("departAfterHour", "After 8 AM");
    }
    var before = /\bbefore\s*(\d{1,2})(?::\d{2})?\s*(am|pm)?/.exec(q);
    if (before) {
      var h2 = before[2] ? hourFrom(before[1], before[2]) : parseInt(before[1], 10);
      filterState.departBeforeHour = h2;
      add("departBeforeHour", "Before " + esc(before[1]) + (before[2] ? " " + before[2].toUpperCase() : ""));
    }

    var namedBucket = /\b(?:early )?morning\b/.test(q) ? "morning" :
      /\b(?:late )?afternoon\b|\bafter lunch\b/.test(q) ? "afternoon" :
      /\b(?:early |late )?evening\b/.test(q) ? "evening" :
      /\b(?:night|red[ -]?eye)\b/.test(q) ? "night" : null;
    if (namedBucket && !/\b(?:no|not|avoid|skip)\s+(?:a\s+)?(?:red[ -]?eye|night|early morning)\b/.test(q)) {
      filterState.depart.morning = false;
      filterState.depart.afternoon = false;
      filterState.depart.evening = false;
      filterState.depart.night = false;
      filterState.depart[namedBucket] = true;
      add("depart:" + namedBucket, namedBucket.charAt(0).toUpperCase() + namedBucket.slice(1) + " departures");
    } else if (/\b(?:no|not|avoid|skip)\s+(?:a\s+)?(?:red[ -]?eye|night)\b/.test(q)) {
      filterState.departAfterHour = 5;
      filterState.departBeforeHour = 22;
      add("departAfterHour", "After 5 AM");
      add("departBeforeHour", "Before 10 PM");
    }

    var stageAirlines = Object.keys(filterState.airlines);
    stageAirlines.forEach(function (name) {
      var needle = name.toLowerCase();
      var shortName = needle.split(/\s+/).filter(function (part) { return part.length > 3 && part !== "airline" && part !== "partner"; });
      if (q.indexOf(needle) !== -1 || shortName.some(function (part) { return new RegExp("\\b" + part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b").test(q); })) {
        filterState.airlines[name] = true;
        add("airline:" + name, name);
      }
    });

    if (/\b(?:cheapest|lowest price|lowest fare|best price)\b/.test(q)) {
      activeTab = "cheapest";
      add("sort:cheapest", "Cheapest first");
    } else if (/\b(?:fastest|quickest|shortest trip)\b/.test(q)) {
      activeTab = "fastest";
      add("sort:fastest", "Quickest first");
    }

    return applied;
  }

  function passesNlHourFilter(item) {
    var h = hourOf(item.departIso);
    if (h === null) return true;
    if (filterState.departAfterHour !== null && h < filterState.departAfterHour) return false;
    if (filterState.departBeforeHour !== null && h >= filterState.departBeforeHour) return false;
    return true;
  }

  function renderAppliedNlChips() {
    if (!aiFilterApplied) return;
    if (!appliedNlFilters.length) { aiFilterApplied.hidden = true; aiFilterApplied.innerHTML = ""; return; }
    aiFilterApplied.hidden = false;
    aiFilterApplied.innerHTML =
      '<div class="lab-ai-filter-applied-label"><span class="lab-spark" aria-hidden="true">✦</span> Skairova applied ' + appliedNlFilters.length + " filter" + (appliedNlFilters.length === 1 ? "" : "s") + "</div>" +
      '<div class="lab-ai-filter-chips">' +
        appliedNlFilters.map(function (f) {
          return '<span class="lab-ai-filter-chip">' + esc(f.label) + ' <button type="button" data-nl-remove="' + esc(f.key) + '" aria-label="Remove filter">×</button></span>';
        }).join("") +
      "</div>" +
      '<button type="button" class="lab-ai-filter-undo" id="labAiFilterUndo">Undo</button>';
  }

  function removeNlFilter(key) {
    appliedNlFilters = appliedNlFilters.filter(function (f) { return f.key !== key; });
    if (key === "stops") { filterState.stops[0] = false; filterState.stops[1] = false; filterState.stops[2] = false; syncQuickChips(); [0, 1, 2].forEach(function (b) { stopInputs[b].checked = filterState.stops[b]; }); }
    else if (key === "maxPrice") { filterState.maxPrice = Infinity; }
    else if (key === "minPrice") { filterState.minPrice = 0; }
    else if (key === "maxDuration") { filterState.maxDuration = Infinity; }
    else if (key === "departAfterHour") filterState.departAfterHour = null;
    else if (key === "departBeforeHour") filterState.departBeforeHour = null;
    else if (key.indexOf("sort:") === 0) {
      activeTab = "best";
      syncSortControl();
    }
    else if (key.indexOf("depart:") === 0) {
      var bucket = key.slice(7);
      filterState.depart[bucket] = false;
      if (departInputs[bucket]) departInputs[bucket].checked = false;
    }
    else if (key.indexOf("airline:") === 0) {
      var name = key.slice(8);
      filterState.airlines[name] = false;
      var input = airlineListEl.querySelector('[data-lab-filter-airline="' + name + '"]');
      if (input) input.checked = false;
    }
    renderAppliedNlChips();
    renderStage();
  }

  if (aiFilterApplied) {
    aiFilterApplied.addEventListener("click", function (e) {
      var removeBtn = e.target.closest("[data-nl-remove]");
      if (removeBtn) { removeNlFilter(removeBtn.dataset.nlRemove); return; }
      if (e.target.id === "labAiFilterUndo") {
        appliedNlFilters = [];
        if (aiFilterInput) aiFilterInput.value = "";
        clearAllFilters();
        renderAppliedNlChips();
      }
    });
  }

  function runAiFilter() {
    if (!aiFilterInput) return;
    var text = aiFilterInput.value.trim();
    if (!text) return;
    var applied = parseAiFilterText(text);
    if (!applied.length) {
      if (aiFilterApplied) {
        aiFilterApplied.hidden = false;
        aiFilterApplied.innerHTML = '<div class="lab-ai-filter-empty">Try a price, stops, airline, or time — for example “nonstop under $400”.</div>';
      }
      return;
    }
    appliedNlFilters = applied;
    [0, 1, 2].forEach(function (bucket) {
      if (stopInputs[bucket]) stopInputs[bucket].checked = filterState.stops[bucket];
    });
    ["morning", "afternoon", "evening", "night"].forEach(function (bucket) {
      if (departInputs[bucket]) departInputs[bucket].checked = filterState.depart[bucket];
    });
    syncQuickChips();
    renderAppliedNlChips();
    renderStage();
  }
  if (aiFilterGo) aiFilterGo.addEventListener("click", runAiFilter);
  if (aiFilterInput) {
    aiFilterInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); runAiFilter(); }
    });
  }

  /* ---------------- edit search ---------------- */

  var editBtn = document.getElementById("labEditSearch");
  var editorEl = document.getElementById("labSearchEditor");
  var editorForm = document.getElementById("labSearchEditorForm");
  if (editBtn && editorEl && editorForm) {
    var originInput = document.getElementById("labEditorOrigin");
    var destinationInput = document.getElementById("labEditorDestination");
    var originOptions = document.getElementById("labEditorOriginOptions");
    var destinationOptions = document.getElementById("labEditorDestinationOptions");
    var swapBtn = document.getElementById("labEditorSwap");
    var tripTypeInput = document.getElementById("labEditorTripType");
    var returnInput = document.getElementById("labEditorReturn");
    var departInput = document.getElementById("labEditorDepart");
    var editorError = document.getElementById("labSearchEditorError");
    var lastFocused = null;
    var airportAbort = null;
    var airportTimer = null;
    var calendarInitAttempts = 0;

    function closeAirportOptions() {
      [originOptions, destinationOptions].forEach(function (box) {
        if (box) { box.hidden = true; box.textContent = ""; }
      });
      [originInput, destinationInput].forEach(function (input) {
        if (input) input.setAttribute("aria-expanded", "false");
      });
    }

    function closeEditor() {
      if (airportAbort) airportAbort.abort();
      if (airportTimer) window.clearTimeout(airportTimer);
      closeAirportOptions();
      editorEl.hidden = true;
      document.body.classList.remove("lab-editor-open");
      if (lastFocused) lastFocused.focus();
    }

    function openEditor() {
      lastFocused = document.activeElement;
      if (editorError) editorError.hidden = true;
      editorEl.hidden = false;
      document.body.classList.add("lab-editor-open");
      ensureLabDateCalendar();
      window.setTimeout(function () { if (originInput) originInput.focus(); }, 0);
    }

    function setTripType(type) {
      var roundtrip = type === "roundtrip";
      if (tripTypeInput) {
        tripTypeInput.value = roundtrip ? "roundtrip" : "oneway";
        tripTypeInput.dispatchEvent(new Event("change", { bubbles: true }));
      }
      editorEl.querySelectorAll("[data-lab-editor-trip]").forEach(function (button) {
        button.setAttribute("aria-selected", String(button.getAttribute("data-lab-editor-trip") === (roundtrip ? "roundtrip" : "oneway")));
      });
      if (returnInput) {
        returnInput.disabled = !roundtrip;
        returnInput.required = roundtrip;
      }
    }

    function labDateLabel(isoDate) {
      var parts = String(isoDate || "").split("-");
      if (parts.length !== 3) return "";
      var date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
      return isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" }).format(date);
    }

    function syncLabDateLabels() {
      var departDisplay = document.getElementById("labEditorDepartDisplay");
      var returnDisplay = document.getElementById("labEditorReturnDisplay");
      var departLabel = labDateLabel(departInput && departInput.value);
      var returnLabel = labDateLabel(returnInput && returnInput.value);
      if (departDisplay) departDisplay.value = returnLabel ? departLabel + " – " + returnLabel : departLabel;
      if (returnDisplay) returnDisplay.value = returnLabel;
    }

    function bindLabDateLabels() {
      if (!departInput || departInput.dataset.labDateLabelsBound === "true") return;
      departInput.dataset.labDateLabelsBound = "true";
      [departInput, returnInput].forEach(function (input) {
        if (!input) return;
        input.addEventListener("input", syncLabDateLabels);
        input.addEventListener("change", syncLabDateLabels);
      });
      ["labEditorDepartDisplay", "labEditorReturnDisplay"].forEach(function (id) {
        var displayInput = document.getElementById(id);
        if (!displayInput) return;
        displayInput.addEventListener("input", syncLabDateLabels);
        displayInput.addEventListener("change", syncLabDateLabels);
      });
      syncLabDateLabels();
    }

    function ensureLabDateCalendar() {
      if (!departInput || !returnInput) return;
      if (typeof window.initNativeDateRange !== "function") {
        if (calendarInitAttempts++ < 24) window.setTimeout(ensureLabDateCalendar, 50);
        return;
      }
      window.initNativeDateRange(departInput, returnInput, {
        tripTypeInput: tripTypeInput,
        departPrompt: "Choose your departure date",
        returnPrompt: "Choose your return date"
      });
      bindLabDateLabels();
    }

    function fitInlineSelect(select) {
      var option = select && select.options[select.selectedIndex];
      if (!option) return;
      var measure = document.createElement("span");
      var style = window.getComputedStyle(select);
      measure.textContent = option.text.trim();
      measure.style.cssText = "position:fixed;visibility:hidden;white-space:nowrap;left:-9999px;top:-9999px;font:" + style.font + ";letter-spacing:" + style.letterSpacing + ";";
      document.body.appendChild(measure);
      var width = Math.ceil(measure.getBoundingClientRect().width) + 2;
      measure.remove();
      select.style.width = Math.max(42, width) + "px";
    }

    function selectAirport(input, box, item) {
      input.value = String(item.code || "").toUpperCase();
      input.dispatchEvent(new Event("change", { bubbles: true }));
      box.hidden = true;
      box.textContent = "";
      input.setAttribute("aria-expanded", "false");
    }

    function renderAirportOptions(input, box, items) {
      box.textContent = "";
      (items || []).slice(0, 6).forEach(function (item) {
        var option = document.createElement("button");
        option.type = "button";
        option.className = "lab-editor-airport-option";
        option.setAttribute("role", "option");
        var code = document.createElement("span");
        code.className = "lab-editor-airport-code";
        code.textContent = String(item.code || "").toUpperCase();
        var label = document.createElement("span");
        label.className = "lab-editor-airport-label";
        label.textContent = String(item.label || item.code || "").replace(String(item.code || "") + " — ", "");
        option.appendChild(code);
        option.appendChild(label);
        option.addEventListener("click", function () { selectAirport(input, box, item); });
        box.appendChild(option);
      });
      box.hidden = !box.childElementCount;
      input.setAttribute("aria-expanded", String(!box.hidden));
    }

    function attachAirportSuggestions(input, box) {
      if (!input || !box) return;
      input.addEventListener("input", function () {
        var query = input.value.trim();
        if (airportTimer) window.clearTimeout(airportTimer);
        if (airportAbort) airportAbort.abort();
        if (query.length < 3 || !searchForm.airportSuggestUrl) {
          box.hidden = true;
          box.textContent = "";
          input.setAttribute("aria-expanded", "false");
          return;
        }
        airportTimer = window.setTimeout(function () {
          airportAbort = new AbortController();
          fetch(searchForm.airportSuggestUrl + "?q=" + encodeURIComponent(query), { signal: airportAbort.signal })
            .then(function (response) { return response.ok ? response.json() : []; })
            .then(function (items) {
              if (document.activeElement === input) renderAirportOptions(input, box, items);
            })
            .catch(function (error) {
              if (error && error.name !== "AbortError") closeAirportOptions();
            });
        }, 160);
      });
      input.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { closeAirportOptions(); input.blur(); }
      });
    }

    editBtn.addEventListener("click", openEditor);
    editorEl.querySelectorAll("[data-lab-editor-close]").forEach(function (button) {
      button.addEventListener("click", closeEditor);
    });
    editorEl.querySelectorAll("[data-lab-editor-trip]").forEach(function (button) {
      button.addEventListener("click", function () { setTripType(button.getAttribute("data-lab-editor-trip")); });
    });
    if (swapBtn) swapBtn.addEventListener("click", function () {
      var origin = originInput.value;
      originInput.value = destinationInput.value;
      destinationInput.value = origin;
      originInput.focus();
    });
    editorEl.querySelectorAll(".lab-editor-inline-select select").forEach(function (select) {
      fitInlineSelect(select);
      select.addEventListener("change", function () { fitInlineSelect(select); });
    });
    attachAirportSuggestions(originInput, originOptions);
    attachAirportSuggestions(destinationInput, destinationOptions);
    document.addEventListener("click", function (event) {
      if (!editorEl.hidden && !event.target.closest(".lab-editor-field")) closeAirportOptions();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !editorEl.hidden) closeEditor();
    });
    editorForm.addEventListener("submit", function (event) {
      var origin = originInput.value.trim();
      var destination = destinationInput.value.trim();
      var isRoundTrip = tripTypeInput && tripTypeInput.value === "roundtrip";
      var departDate = departInput.value;
      var returnDate = isRoundTrip ? returnInput.value : "";
      var message = !origin || !destination ? "Add both an origin and a destination." :
        (!departDate ? "Choose a departure date." :
          (isRoundTrip && !returnDate ? "Choose a return date." :
            (isRoundTrip && returnDate < departDate ? "Your return date must be after your departure date." : "")));
      if (message) {
        event.preventDefault();
        if (editorError) { editorError.textContent = message; editorError.hidden = false; }
        return;
      }
    });
    setTripType((tripTypeInput && tripTypeInput.value) || "roundtrip");
    ensureLabDateCalendar();
  }

  /* ---------------- initial paint ---------------- */

  renderStage();
})();
