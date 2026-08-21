/* ============================================================
   Skairova AI Assistant — flight-aware chatbot + insight banner + AI filter
   ============================================================ */
(function () {
  "use strict";

  const MAX_CHARS = 400;
  const CHAR_NEAR = 360;

  /* ── Module-level flight state ── */
  let _allFlights = [];          // all visible flight cards, structured
  let _focusedFlight = null;     // card the user clicked Details/Select on
  let _focusMode = "none";       // "none" | "details" | "selected"
  let _updateFocusUI = null;     // injected by initChatbot
  let _addSystemMsg = null;      // injected by initChatbot
  let _greetingBubble = null;    // first AI bubble element (results page enrichment)
  let _chatHasInteracted = false; // true once user sends first message

  /* ─────────────────────────────────────────────────────────
     FLIGHT DATA EXTRACTION
  ───────────────────────────────────────────────────────── */

  function _durationStr(min) {
    if (!min || isNaN(min)) return "";
    const h = Math.floor(min / 60);
    const m = min % 60;
    return m ? `${h}h ${m}m` : `${h}h`;
  }

  function extractFlightFromCard(card, rankOverride) {
    const rank = rankOverride != null ? rankOverride : (
      parseInt(card.className.match(/rank-(\d+)/)?.[1] || "0", 10) || 0
    );

    const airline   = card.querySelector(".fc-carrier-name")?.textContent.trim() || "";
    const priceRaw  = parseFloat(card.dataset.price || "0") || 0;
    const price     = priceRaw ? `$${Math.round(priceRaw)}` : "";
    const durMin    = parseInt(card.dataset.durationMin || "0", 10);
    const duration  = _durationStr(durMin);
    const stopCount = parseInt(card.dataset.totalStops || "0", 10);
    const badges    = Array.from(card.querySelectorAll(".fc-badge, .fc-ai-note")).map(b => b.textContent.trim()).filter(Boolean);
    const smartBadge = card.querySelector(".fc-ai-note")?.textContent.trim() || "";

    // Departure ISO → human date
    const departIso  = card.dataset.outDepartIso || "";
    const arriveIso  = card.dataset.outArriveIso || "";
    const departDate = departIso ? departIso.split("T")[0] : "";
    const departTime = departIso && departIso.includes("T") ? departIso.split("T")[1]?.slice(0, 5) : "";
    const arriveTime = arriveIso && arriveIso.includes("T") ? arriveIso.split("T")[1]?.slice(0, 5) : "";

    const originCode = (card.dataset.outOrigin || "").toUpperCase();
    const destCode   = (card.dataset.outDest   || "").toUpperCase();

    // Extract per-segment detail from rendered fc-seg elements
    const segments = [];
    card.querySelectorAll(".fc-seg").forEach(seg => {
      const times  = Array.from(seg.querySelectorAll(".fc-time")).map(t => t.textContent.trim());
      const iatas  = Array.from(seg.querySelectorAll(".fc-iata")).map(i => i.textContent.trim());
      const segDur = seg.querySelector(".fc-via-dur")?.textContent.replace("Travel", "").trim() || "";
      const segStops = seg.querySelector(".fc-via-stops")?.textContent.trim() || "";
      const layoverCodes = Array.from(seg.querySelectorAll(".fc-via-dot")).map((_, idx) => {
        // try to get layover info from title attribute on the layover span
        return "";
      });
      const layoverSpan = seg.querySelector(".fc-via-layover");
      const layoverText = layoverSpan ? layoverSpan.textContent.trim() : "";
      if (iatas.length >= 2) {
        segments.push({
          from:      iatas[0] || "",
          to:        iatas[1] || "",
          depart:    times[0] || "",
          arrive:    times[1] || "",
          duration:  segDur,
          stops:     segStops,
          layovers:  layoverText,
        });
      }
    });

    // Fare note: price vs cheapest
    const priceVsCheapest = parseFloat(card.querySelector("[data-initial-delta]")?.dataset.initialDelta || "0") || 0;
    const priceNote = priceVsCheapest > 0.5 ? `+$${Math.round(priceVsCheapest)} vs cheapest` : "Lowest price";

    return {
      rank,
      airline,
      price,
      price_raw: priceRaw,
      price_note: priceNote,
      duration,
      duration_min: durMin,
      stops: stopCount === 0 ? "Nonstop" : stopCount === 1 ? "1 stop" : `${stopCount} stops`,
      stop_count: stopCount,
      depart_date: departDate,
      depart_time: departTime,
      arrive_time: arriveTime,
      origin: originCode,
      destination: destCode,
      badges,
      smart_badge: smartBadge,
      segments,
    };
  }

  function extractAllFlights() {
    const cards = Array.from(document.querySelectorAll(
      ".flight-card:not(.flex-live-skeleton):not([aria-hidden='true'])"
    )).filter(c => !c.classList.contains("flex-live-skeleton"));

    return cards.map((card, idx) => extractFlightFromCard(card, idx + 1));
  }

  function refreshAllFlights() {
    _allFlights = extractAllFlights();
    // Enrich results-page greeting bubble with live price data (if chat hasn't been used yet)
    if (_greetingBubble && !_chatHasInteracted && _allFlights.length > 0) {
      const cheapest = _allFlights.reduce((m, f) => (!m || (f.price_raw > 0 && f.price_raw < m.price_raw)) ? f : m, null);
      const dest = (window.SKAIR_PAGE || {}).destination || "";
      const destStr = dest ? ` to ${dest}` : "";
      if (cheapest && cheapest.price && cheapest.airline) {
        _greetingBubble.textContent =
          `${_allFlights.length} flights found${destStr}. ${cheapest.airline} from ${cheapest.price} — ask me about any option, the airlines, fare differences, or what to expect.`;
      }
    }
  }

  /* Watch for cards added by streaming */
  function watchFlightCards() {
    const container = document.querySelector(".cards, #resultsCards");
    if (!container) return;
    const obs = new MutationObserver(() => {
      refreshAllFlights();
    });
    obs.observe(container, { childList: true, subtree: false });
  }

  /* Listen for card interactions */
  function initFlightTracking() {
    document.addEventListener("click", (e) => {
      // Details toggle
      const dtlBtn = e.target.closest(".fc-dtl-btn");
      if (dtlBtn) {
        const card = dtlBtn.closest(".flight-card");
        if (!card) return;
        const isExpanding = dtlBtn.getAttribute("aria-expanded") !== "true";
        if (isExpanding) {
          const flight = extractFlightFromCard(card);
          setFocusedFlight(flight, "details");
        }
        return;
      }

      // Select / Book button
      const bookBtn = e.target.closest(".fc-book");
      if (bookBtn && !bookBtn.classList.contains("booking-unavailable")) {
        const card = bookBtn.closest(".flight-card");
        if (!card) return;
        const flight = extractFlightFromCard(card);
        setFocusedFlight(flight, "selected");
        return;
      }
    });
  }

  function setFocusedFlight(flight, mode) {
    _focusedFlight = flight;
    _focusMode = mode || "details";
    if (_updateFocusUI) _updateFocusUI(flight, mode);
    if (mode === "selected" && _addSystemMsg) {
      _addSystemMsg(`Selected: ${flight.airline} · ${flight.price} · ${flight.stops} · ${flight.duration}`);
    } else if (mode === "details" && _addSystemMsg) {
      _addSystemMsg(`Now viewing: ${flight.airline} · ${flight.price} · ${flight.stops} · ${flight.duration}`);
    }
  }

  /* ─────────────────────────────────────────────────────────
     CONTEXT BUILDER (search params + all flights + focused)
  ───────────────────────────────────────────────────────── */

  function collectContext() {
    // Read page context from embedded JSON element (avoids JS linting issues with Jinja2)
    let page = window.SKAIR_PAGE || {};
    try {
      const el = document.getElementById("skairPageCtx");
      if (el) page = Object.assign(page, JSON.parse(el.textContent || "{}"));
    } catch (e) { /* ignore parse errors */ }
    const ctx = Object.assign({}, page);

    // Also pull search form fields (results page — doesn't override page context)
    const form = document.querySelector("#srbEditForm, form[action*='/search']");
    const keys = ["origin", "destination", "depart_date", "return_date", "cabin", "passengers", "trip_type", "flex_month"];
    keys.forEach(k => {
      const el = form ? form.querySelector(`[name="${k}"]`) : null;
      if (el && el.value && !ctx[k]) ctx[k] = el.value;
    });
    const hidden = document.querySelectorAll(
      "#flexStreamBootForm input[type=hidden], #resultsPendingBootForm input[type=hidden]"
    );
    hidden.forEach(el => { if (el.name && !ctx[el.name]) ctx[el.name] = el.value; });

    // Attach live flight data (results page only)
    const flights = _allFlights.length ? _allFlights : extractAllFlights();
    if (flights.length) ctx.flights = flights.slice(0, 12);
    if (_focusedFlight) {
      ctx.focused_flight = _focusedFlight;
      ctx.focus_mode = _focusMode;
    }

    return ctx;
  }

  /* ── Build per-page quick-reply chips — data-driven from live context ── */
  function buildQuickReplies(ctx) {
    const page    = ctx.page_type || "results";
    const dest    = ctx.destination || "";
    const airline = ctx.airline_name || ctx.airline || "";
    const flights = ctx.flights || [];

    // Derive real stats from live flight data
    let cheapest = null, fastest = null;
    flights.forEach(f => {
      if (!cheapest || (f.price_raw > 0 && f.price_raw < cheapest.price_raw)) cheapest = f;
      if (!fastest  || (f.duration_min > 0 && f.duration_min < fastest.duration_min)) fastest = f;
    });
    const topAirline   = flights[0]?.airline || "";
    const cheapAirline = cheapest?.airline   || "";
    const cheapPrice   = cheapest?.price     || "";

    if (page === "checkout") {
      return [
        airline ? `What's ${airline}'s baggage policy?` : "What's the baggage allowance?",
        "What documents do I need for check-in?",
        "Is travel insurance worth adding?",
        "Can I change this booking later?",
      ];
    }

    if (page === "seat_selection") {
      return [
        "Which seats have the most legroom?",
        "Are exit row seats worth the extra cost?",
        airline ? `Any seat tips for ${airline}?` : "Best seats for a window view?",
        "Can I change my seat after booking?",
      ];
    }

    if (page === "confirmation") {
      const chips = ["How early should I arrive at the airport?"];
      if (dest) chips.push(`Do I need a visa for ${dest}?`);
      else      chips.push("Is online check-in available?");
      chips.push("What's my baggage allowance?");
      if (dest) chips.push(`Any tips for visiting ${dest}?`);
      else      chips.push("What should I know before I fly?");
      return chips;
    }

    if (page === "manage_booking") {
      return [
        "Can I change the date on my flight?",
        "How do refunds work for this fare?",
        "What happens if I miss my flight?",
        "How do I add extra baggage?",
      ];
    }

    if (page === "booking_review") {
      return [
        airline ? `Is ${airline} a reliable airline?`        : "What's included in this fare?",
        "What's the cancellation policy?",
        "How does baggage work?",
        dest    ? `What's flying into ${dest} like?`         : "Any tips for this route?",
      ];
    }

    // Results page — use actual live flight data
    const replies = [];
    if (cheapAirline && cheapPrice) {
      replies.push(`Is ${cheapAirline} reliable for this route?`);
      replies.push(`What's included in the ${cheapPrice} fare?`);
    } else if (dest) {
      replies.push(`What's ${dest} like to visit?`);
      replies.push("Which flight gives the best value?");
    } else {
      replies.push("Which flight gives the best value?");
    }
    if (topAirline && topAirline !== cheapAirline) {
      replies.push(`How good is ${topAirline} on this route?`);
    } else if (dest && !replies.some(r => r.includes(dest))) {
      replies.push(`Best time of year to visit ${dest}?`);
    } else {
      replies.push("Best seat tips for a long-haul flight?");
    }
    if (fastest && cheapest && fastest !== cheapest && fastest.airline) {
      replies.push(`Is the faster ${fastest.airline} option worth the extra?`);
    } else if (!replies.some(r => r.includes("visa") || r.includes("airport"))) {
      replies.push("How early should I arrive at the airport?");
    }
    return replies.slice(0, 4);
  }

  /* ─────────────────────────────────────────────────────────
     AI INSIGHT BANNER
  ───────────────────────────────────────────────────────── */

  window.SkairAIInsight = {
    _shown: false,
    trigger: function (flightData) {
      if (this._shown) return;
      this._shown = true;
      const outer = document.getElementById("aiInsightOuter");
      const banner = document.getElementById("aiInsightBanner");
      if (!banner || !outer) return;
      const textEl = banner.querySelector(".ai-insight-text");
      if (textEl) {
        textEl.classList.add("is-loading");
        textEl.innerHTML = '<span class="ai-insight-spinner"></span>Analyzing your results…';
      }
      outer.classList.add("is-visible");

      const ctx = collectContext();
      if (flightData) {
        if (flightData.cheapest_price)       ctx.cheapest_price       = flightData.cheapest_price;
        if (flightData.cheapest_date)        ctx.cheapest_date        = flightData.cheapest_date;
        if (flightData.best_airline)         ctx.best_airline         = flightData.best_airline;
        if (flightData.fastest_duration_min) ctx.fastest_duration_min = flightData.fastest_duration_min;
        if (flightData.total_results)        ctx.total_results        = flightData.total_results;
      }

      fetch("/api/ai/insight", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
        body: JSON.stringify(ctx),
      })
        .then(r => r.json())
        .then(data => {
          if (textEl) {
            textEl.classList.remove("is-loading");
            const raw = data.insight || "Your results are ready.\nCheck the top cards for our best picks.";
            const highlightPrices = s => s.replace(/(\$[\d,]+(?:\.\d{1,2})?)/g, '<strong class="ai-insight-price">$1</strong>');
            const lines = raw.split('\n').map(l => l.trim()).filter(Boolean);
            if (lines.length >= 2) {
              textEl.innerHTML =
                `<span class="ai-insight-headline">${highlightPrices(lines[0])}</span>` +
                `<span class="ai-insight-detail">${highlightPrices(lines.slice(1).join(' '))}</span>`;
            } else {
              textEl.innerHTML = `<span class="ai-insight-headline">${highlightPrices(raw)}</span>`;
            }
          }
        })
        .catch(() => {
          if (textEl) {
            textEl.classList.remove("is-loading");
            textEl.textContent = "Your results are ready. Check the top cards for our best picks.";
          }
        });
    },
  };

  function extractFlightSummary() {
    const cards = document.querySelectorAll(".flight-card:not(.flex-live-skeleton)");
    let cheapest = Infinity, cheapestDate = null, bestAirline = null, fastestMin = Infinity;
    cards.forEach((card, i) => {
      const price = parseFloat(card.dataset.price || "");
      const dur   = parseInt(card.dataset.durationMin || "", 10);
      if (!isNaN(price) && price < cheapest) {
        cheapest = price;
        const iso = card.dataset.outDepartIso || "";
        if (iso) cheapestDate = iso.split("T")[0];
      }
      if (!isNaN(dur) && dur < fastestMin) fastestMin = dur;
      if (i === 0) bestAirline = card.querySelector(".fc-carrier-name")?.textContent.trim() || null;
    });
    return {
      cheapest_price:       cheapest < Infinity ? cheapest : null,
      cheapest_date:        cheapestDate,
      best_airline:         bestAirline,
      fastest_duration_min: fastestMin < Infinity ? fastestMin : null,
      total_results:        cards.length,
    };
  }

  function maybeAutoTriggerInsight() {
    if (document.getElementById("flexStreamBootForm") || document.getElementById("resultsPendingBootForm")) return;
    const banner = document.getElementById("aiInsightBanner");
    if (!banner) return;
    if (document.querySelectorAll(".flight-card:not(.flex-live-skeleton)").length === 0) return;
    const summary = extractFlightSummary();
    try {
      const stored = sessionStorage.getItem("skair_flex_insight");
      if (stored) {
        const parsed = JSON.parse(stored);
        if (parsed && Date.now() - (parsed.ts || 0) < 120000) {
          if (parsed.cheapest_price && !summary.cheapest_price) summary.cheapest_price = parsed.cheapest_price;
          if (parsed.cheapest_date  && !summary.cheapest_date)  summary.cheapest_date  = parsed.cheapest_date;
        }
        sessionStorage.removeItem("skair_flex_insight");
      }
    } catch (e) { /* ignore */ }
    setTimeout(() => window.SkairAIInsight.trigger(summary), 800);
  }

  /* ─────────────────────────────────────────────────────────
     AI NATURAL LANGUAGE FILTER
  ───────────────────────────────────────────────────────── */

  function initAiFilter() {
    const wrap = document.getElementById("aiFilterWrap");
    if (!wrap) return;
    const input  = wrap.querySelector(".ai-filter-input");
    const btn    = wrap.querySelector(".ai-filter-btn");
    const status = wrap.querySelector(".ai-filter-status");
    const chips  = wrap.querySelector(".ai-filter-chips");
    if (!input || !btn) return;

    ["Nonstop under $400", "Morning departure", "Fastest flight", "Best value"].forEach(p => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "ai-filter-chip";
      chip.textContent = p;
      chip.addEventListener("click", () => { input.value = p; applyFilter(); });
      chips?.appendChild(chip);
    });

    const applyFilter = () => {
      const q = input.value.trim();
      if (!q) return;
      btn.classList.add("is-loading");
      btn.disabled = true;
      if (status) { status.textContent = "Parsing…"; status.className = "ai-filter-status"; }
      fetch("/api/ai/filter", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
        body: JSON.stringify({ query: q }),
      })
        .then(r => r.json())
        .then(data => {
          const f = data.filters || {};
          applyParsedFilters(f);
          if (status) {
            const parts = [];
            if (f.max_price != null)       parts.push(`max $${f.max_price}`);
            if (f.max_stops != null)       parts.push(f.max_stops === 0 ? "nonstop" : `≤${f.max_stops} stop`);
            if (f.sort)                    parts.push(`sort: ${f.sort}`);
            if (f.departure_times?.length) parts.push(`depart: ${f.departure_times.join(", ")}`);
            if (f.max_duration_min != null) parts.push(`max ${Math.round(f.max_duration_min / 60)}h`);
            status.textContent = parts.length ? `Applied: ${parts.join(" · ")}` : "Filters applied";
            status.className = "ai-filter-status is-success";
          }
        })
        .catch(() => {
          if (status) { status.textContent = "Couldn't parse. Try simpler phrasing."; status.className = "ai-filter-status is-error"; }
        })
        .finally(() => { btn.classList.remove("is-loading"); btn.disabled = false; });
    };

    btn.addEventListener("click", applyFilter);
    input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); applyFilter(); } });
  }

  function applyParsedFilters(f) {
    if (!f || typeof f !== "object") return;
    if (f.sort) {
      const s = { cheapest: "cheapest", fastest: "fastest", best: "best", recommended: "best", earliest_departure: "earliest_departure", earliest_arrival: "earliest_arrival", fewest_stops: "fewest_stops" }[f.sort];
      if (s) {
        document.querySelectorAll("[data-results-sort]").forEach(btn => {
          btn.classList.toggle("is-active", btn.dataset.resultsSort === s);
          btn.setAttribute("aria-selected", btn.dataset.resultsSort === s ? "true" : "false");
        });
        document.querySelectorAll("[data-results-sort-select]").forEach(sel => {
          sel.value = s;
          sel.dispatchEvent(new Event("change", { bubbles: true }));
        });
      }
    }
    if (f.max_stops != null) {
      document.querySelectorAll(".results-filter-stop").forEach(cb => {
        cb.checked = parseInt(cb.value, 10) <= f.max_stops;
        cb.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }
    if (f.departure_times?.length) {
      document.querySelectorAll(".results-filter-departure").forEach(cb => {
        cb.checked = f.departure_times.includes(cb.value);
        cb.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }
    if (f.arrival_times?.length) {
      document.querySelectorAll(".results-filter-arrival").forEach(cb => {
        cb.checked = f.arrival_times.includes(cb.value);
        cb.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }
    if (f.max_price != null) {
      const el = document.getElementById("filterPriceRangeMax");
      if (el) { el.value = Math.min(f.max_price, parseInt(el.max, 10)); el.dispatchEvent(new Event("input", { bubbles: true })); }
    }
    if (f.max_duration_min != null) {
      const el = document.getElementById("filterDurationMax");
      if (el) { el.value = Math.min(f.max_duration_min, parseInt(el.max, 10)); el.dispatchEvent(new Event("input", { bubbles: true })); }
    }
    if (typeof window.ResultsPage?.applyFilters === "function") window.ResultsPage.applyFilters();
    else document.querySelectorAll(".results-filter-stop, .results-filter-departure, .results-filter-arrival").forEach(el => el.dispatchEvent(new Event("change", { bubbles: true })));
  }

  /* ─────────────────────────────────────────────────────────
     AI CHATBOT
  ───────────────────────────────────────────────────────── */

  function initChatbot() {
    const wrap       = document.getElementById("aiChatBubbleWrap");
    if (!wrap) return;
    const trigger    = wrap.querySelector(".ai-chat-trigger");
    const panel      = wrap.querySelector(".ai-chat-panel");
    const msgs       = wrap.querySelector(".ai-chat-messages");
    const input      = wrap.querySelector(".ai-chat-input");
    const sendBtn    = wrap.querySelector(".ai-chat-send");
    const charCount  = wrap.querySelector(".ai-chat-char-count");
    const qrWrap     = wrap.querySelector(".ai-quick-replies");
    const badge      = wrap.querySelector(".ai-chat-badge");
    const focusBar   = wrap.querySelector(".ai-chat-focus-bar");
    const focusLabel = wrap.querySelector(".ai-chat-focus-label");
    const focusClear = wrap.querySelector(".ai-chat-focus-clear");
    const newChatBtn = wrap.querySelector(".ai-chat-new");
    const modelTrigger = wrap.querySelector(".ai-chat-model-trigger");
    const modelMenu = wrap.querySelector(".ai-chat-model-menu");
    const modelName = wrap.querySelector(".ai-chat-model-name");
    const modelOptions = Array.from(wrap.querySelectorAll(".ai-chat-model-option"));
    const reasoningOptions = Array.from(wrap.querySelectorAll(".ai-chat-reasoning-option"));
    const headerReasoning = wrap.querySelector("[data-ai-header-reasoning]");
    const detailName = wrap.querySelector("[data-ai-model-detail-name]");
    const detailProvider = wrap.querySelector("[data-ai-model-detail-provider]");
    const detailDescription = wrap.querySelector("[data-ai-model-detail-description]");
    const reasoningCurrent = wrap.querySelector("[data-ai-reasoning-current]");
    const reasoningCopy = wrap.querySelector("[data-ai-reasoning-copy]");
    if (!trigger || !panel || !msgs || !input || !sendBtn) return;

    let isOpen     = false;
    let isWaiting  = false;
    const history  = [];
    let hasInteracted = false;
    let selectedModel = "Skairova";
    let responseStyle = "Balanced";

    /* Wire module-level callbacks */
    _updateFocusUI = function(flight, mode) {
      if (!focusBar || !focusLabel) return;
      if (!flight) { focusBar.style.display = "none"; return; }
      const modeIcon = mode === "selected" ? "✈" : "👁";
      const modeWord = mode === "selected" ? "Selected flight" : "Viewing flight";
      focusLabel.textContent = `${modeIcon} ${modeWord}: ${flight.airline} · ${flight.price} · ${flight.stops}${flight.duration ? " · " + flight.duration : ""}`;
      focusBar.style.display = "flex";
      if (mode === "selected") focusBar.classList.add("is-selected");
      else focusBar.classList.remove("is-selected");
    };

    _addSystemMsg = function(text) {
      const row = document.createElement("div");
      row.className = "ai-system-msg";
      row.textContent = text;
      msgs.appendChild(row);
      msgs.scrollTop = msgs.scrollHeight;
      /* Show badge to signal new context is available — never force-open the panel */
      if (!isOpen && badge) { badge.style.display = "flex"; }
    };

    if (focusClear) {
      focusClear.addEventListener("click", () => {
        _focusedFlight = null;
        _focusMode = "none";
        if (focusBar) focusBar.style.display = "none";
      });
    }

    /* Set page-aware greeting in the static bubble message */
    (function () {
      let _p = window.SKAIR_PAGE || {};
      try { const el = document.getElementById("skairPageCtx"); if (el) _p = Object.assign(_p, JSON.parse(el.textContent || "{}")); } catch(e) {}
      const page = _p.page_type || "results";
      const p = _p;
      const greetingTitle = wrap.querySelector(".ai-chat-greeting-title");
      const greetingBody  = wrap.querySelector(".ai-chat-greeting-sub");
      const firstBubble   = wrap.querySelector(".ai-msg-bubble");

      // Expose greeting bubble to module scope so refreshAllFlights() can enrich it with real prices
      if (page === "results" || !p.page_type) _greetingBubble = firstBubble;

      const greetings = {
        results: {
          title: p.destination ? `Flights to ${p.destination}` : "Flight results",
          sub: "Ask me about any airline shown, fare differences, baggage, or travel tips — I can't run new searches.",
          bubble: p.origin && p.destination
            ? `I can see your ${p.origin} → ${p.destination} results. Ask me about the airlines, fare classes, what to expect, or anything travel related.`
            : p.destination
              ? `Looking at flights to ${p.destination}? Ask me about the airlines, fare differences, or travel tips.`
              : "Ask me about any flight shown — airline quality, fare classes, stops, or what to expect.",
        },
        checkout: {
          title: "Ready to book?",
          sub: "Ask me about baggage, documents, or what to expect — I can't make changes on your behalf.",
          bubble: `You've selected ${p.airline_name || "a flight"}${p.route_summary ? " — " + p.route_summary : ""}. Ask me anything before you confirm.`,
        },
        seat_selection: {
          title: "Picking your seat?",
          sub: "Ask me about legroom, window vs aisle, exit rows, or seats to avoid — I can't select seats for you.",
          bubble: `Choosing a seat on ${p.airline_name || "your flight"}. Ask me which seats are best for comfort, legroom, or window views.`,
        },
        confirmation: {
          title: "Booking confirmed!",
          sub: "Ask me about check-in timing, baggage, visa requirements, or what to pack.",
          bubble: `Your booking is confirmed${p.booking_reference ? " — ref " + p.booking_reference : ""}. Ask me about check-in, baggage, or what to expect at your destination.`,
        },
        manage_booking: {
          title: "Managing your booking?",
          sub: "Ask me about fare rules, cancellation policies, or what to do if plans change — I can't make changes for you.",
          bubble: `I can see your booking${p.booking_reference ? " " + p.booking_reference : ""}. Ask me about refunds, change policies, or what to expect on this route.`,
        },
        booking_review: {
          title: "Reviewing your trip?",
          sub: "Ask me about this fare, baggage, the airline's reputation, or the airports on this route.",
          bubble: `You're reviewing your trip on ${p.airline_name || "this airline"}${p.route_summary ? " — " + p.route_summary : ""}. Ask me anything about this flight.`,
        },
      };
      const g = greetings[page];
      if (g) {
        if (greetingTitle) greetingTitle.textContent = g.title;
        if (greetingBody)  greetingBody.textContent  = g.sub;
        if (firstBubble)   firstBubble.textContent   = g.bubble;
      }
    })();

    /* Quick replies — built lazily on first open so live flight data is available */
    let _qrPopulated = false;
    function populateQuickReplies() {
      if (_qrPopulated || !qrWrap) return;
      _qrPopulated = true;
      const ctx = collectContext();
      buildQuickReplies(ctx).forEach(q => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ai-quick-reply";
        btn.textContent = q;
        btn.addEventListener("click", () => {
          input.value = q;
          send();
          qrWrap.style.display = "none";
        });
        qrWrap.appendChild(btn);
      });
    }

    /* Open / close */
    function open() {
      isOpen = true;
      wrap.classList.add("ai-chat-is-open");
      panel.classList.remove("is-closing");
      panel.classList.add("is-open");
      trigger.setAttribute("aria-expanded", "true");
      if (badge) badge.remove();
      populateQuickReplies();
      setTimeout(() => input.focus(), 200);
    }

    function close() {
      isOpen = false;
      closeModelMenu();
      wrap.classList.remove("ai-chat-is-open");
      panel.classList.remove("is-open");
      panel.classList.add("is-closing");
      trigger.setAttribute("aria-expanded", "false");
      panel.addEventListener("animationend", () => panel.classList.remove("is-closing"), { once: true });
    }

    trigger.addEventListener("click", () => isOpen ? close() : open());
    wrap.querySelector(".ai-chat-header-close")?.addEventListener("click", close);

    function closeModelMenu() {
      if (!modelMenu || modelMenu.hidden) return;
      modelMenu.hidden = true;
      modelTrigger?.setAttribute("aria-expanded", "false");
    }

    function toggleModelMenu() {
      if (!modelMenu) return;
      const nextOpen = modelMenu.hidden;
      modelMenu.hidden = !nextOpen;
      modelTrigger?.setAttribute("aria-expanded", String(nextOpen));
    }

    modelTrigger?.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleModelMenu();
    });

    modelOptions.forEach(option => {
      option.addEventListener("click", () => {
        selectedModel = option.dataset.aiModel || "Skairova";
        if (modelName) modelName.textContent = selectedModel;
        if (detailName) detailName.textContent = selectedModel;
        if (detailProvider) detailProvider.textContent = option.dataset.aiProvider || "Skairova";
        if (detailDescription) detailDescription.textContent = option.dataset.aiDescription || "";
        modelOptions.forEach(item => {
          const active = item === option;
          item.classList.toggle("is-selected", active);
          item.setAttribute("aria-selected", String(active));
        });
        closeModelMenu();
      });
    });

    reasoningOptions.forEach(option => {
      option.addEventListener("click", () => {
        responseStyle = option.dataset.aiReasoning || "Balanced";
        reasoningOptions.forEach(item => item.classList.toggle("is-selected", item === option));
        if (headerReasoning) headerReasoning.textContent = responseStyle;
        if (reasoningCurrent) reasoningCurrent.textContent = responseStyle;
        if (reasoningCopy) {
          reasoningCopy.textContent = {
            Brief: "Answers quickly and keeps the response concise.",
            Balanced: "Balances clear answers with helpful detail.",
            Detailed: "Plans at length and checks important trip details."
          }[responseStyle] || "Balances clear answers with helpful detail.";
        }
      });
    });

    document.addEventListener("click", event => {
      if (modelMenu && !modelMenu.hidden && !modelMenu.contains(event.target) && !modelTrigger?.contains(event.target)) closeModelMenu();
    });

    newChatBtn?.addEventListener("click", () => {
      if (isWaiting) return;
      history.length = 0;
      hasInteracted = false;
      _chatHasInteracted = false;
      msgs.querySelectorAll(".ai-msg, .ai-system-msg").forEach((message, index) => {
        if (index > 0 || message.classList.contains("ai-system-msg")) message.remove();
      });
      if (qrWrap) {
        qrWrap.replaceChildren();
        qrWrap.style.display = "";
      }
      _qrPopulated = false;
      populateQuickReplies();
      input.focus();
    });

    /* Messages */
    function scrollToBottom() { msgs.scrollTop = msgs.scrollHeight; }

    function addMessage(role, text) {
      if (role === "user") history.push({ role: "user", content: text });
      const row = document.createElement("div");
      row.className = `ai-msg ai-msg--${role}`;
      if (role === "ai") {
        const av = document.createElement("div");
        av.className = "ai-msg-avatar";
        av.setAttribute("aria-hidden", "true");
        av.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 0 1 7 7h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-1v1a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-1H2a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1a7 7 0 0 1 7-7h1V5.73c-.6-.34-1-.99-1-1.73a2 2 0 0 1 2-2zM7.5 14a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zm9 0a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3z"/></svg>';
        row.appendChild(av);
      }
      const bubble = document.createElement("div");
      bubble.className = "ai-msg-bubble";
      bubble.textContent = text;
      row.appendChild(bubble);
      msgs.appendChild(row);
      scrollToBottom();
      return bubble;
    }

    function addTypingIndicator() {
      const row = document.createElement("div");
      row.className = "ai-msg ai-msg--ai";
      const av = document.createElement("div");
      av.className = "ai-msg-avatar";
      av.setAttribute("aria-hidden", "true");
      av.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 0 1 7 7h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-1v1a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-1H2a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1a7 7 0 0 1 7-7h1V5.73c-.6-.34-1-.99-1-1.73a2 2 0 0 1 2-2zM7.5 14a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zm9 0a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3z"/></svg>';
      row.appendChild(av);
      const ind = document.createElement("div");
      ind.className = "ai-typing-indicator";
      for (let i = 0; i < 3; i++) { const d = document.createElement("div"); d.className = "ai-typing-dot"; ind.appendChild(d); }
      row.appendChild(ind);
      msgs.appendChild(row);
      scrollToBottom();
      return row;
    }

    /* Char count */
    function updateCharCount() {
      const len = input.value.length;
      if (charCount) {
        charCount.textContent = `${len}/${MAX_CHARS}`;
        charCount.className = "ai-chat-char-count" + (len >= MAX_CHARS ? " is-at-limit" : len >= CHAR_NEAR ? " is-near-limit" : "");
      }
      sendBtn.disabled = len === 0 || isWaiting;
    }

    input.addEventListener("input", () => {
      if (input.value.length > MAX_CHARS) input.value = input.value.slice(0, MAX_CHARS);
      updateCharCount();
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 100) + "px";
    });
    input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!sendBtn.disabled) send(); } });
    sendBtn.addEventListener("click", send);

    function send() {
      const text = input.value.trim();
      if (!text || isWaiting) return;
      if (!hasInteracted) { hasInteracted = true; _chatHasInteracted = true; if (qrWrap) qrWrap.style.display = "none"; }
      addMessage("user", text);
      input.value = "";
      input.style.height = "auto";
      updateCharCount();
      isWaiting = true;
      sendBtn.disabled = true;
      const typingRow = addTypingIndicator();

      /* Always snapshot fresh context including live flights + focused flight */
      const ctx = collectContext();
      ctx.assistant = { model: selectedModel, response_style: responseStyle };

      fetch("/api/ai/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
        body: JSON.stringify({ message: text, context: ctx, history: history.slice(-8) }),
      })
        .then(r => r.json())
        .then(data => {
          typingRow.remove();
          const reply = data.reply || "I'm sorry, I couldn't process that. Please try again.";
          addMessage("ai", reply);
          history.push({ role: "assistant", content: reply });
        })
        .catch(() => {
          typingRow.remove();
          addMessage("ai", "Something went wrong. Please try again in a moment.");
        })
        .finally(() => { isWaiting = false; updateCharCount(); input.focus(); });
    }

    updateCharCount();

    /* Show badge after 4s if still closed */
    if (!isOpen && badge) {
      setTimeout(() => { if (!isOpen) badge.style.display = "flex"; }, 4000);
    }
  }

  /* ── Dismiss banner ── */
  function initDismiss() {
    document.getElementById("aiInsightBanner")
      ?.querySelector(".ai-insight-dismiss")
      ?.addEventListener("click", () => document.getElementById("aiInsightOuter")?.remove());
  }

  /* ─────────────────────────────────────────────────────────
     BOOT
  ───────────────────────────────────────────────────────── */

  function init() {
    refreshAllFlights();
    watchFlightCards();
    initFlightTracking();
    initAiFilter();
    initChatbot();
    initDismiss();
    maybeAutoTriggerInsight();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
