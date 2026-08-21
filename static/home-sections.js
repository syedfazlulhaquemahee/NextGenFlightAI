/*
 * Homepage discovery sections:
 * - "Recent searches": localStorage-backed, functional replay of real past
 *   searches.
 * - "Popular flights near you": real photos, a real nearest-airport origin
 *   from device geolocation (falls back to New York/JFK), and live fares
 *   from the real search backend for both an international and a domestic
 *   tab — never fabricated, a route with no live result just shows without
 *   a price badge instead of a fake number.
 */
(function () {
  "use strict";

  var RECENT_KEY = "skair_recent_searches";
  var MAX_RECENT = 6;
  var ISO_RE = /^\d{4}-\d{2}-\d{2}$/;

  var AI_ICON_SVG = "/static/icons/sparkles.svg";
  var ROUTE_ICON_SVG = "/static/icons/plane-takeoff.svg";
  var REPLAY_ICON_SVG = "/static/icons/arrow-right.svg";

  function readRecent() {
    try {
      var raw = localStorage.getItem(RECENT_KEY);
      var list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch (e) {
      return [];
    }
  }

  function writeRecent(list) {
    try {
      localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, MAX_RECENT)));
    } catch (e) {}
  }

  function keyFor(entry) {
    if (entry.mode === "ai") return "ai:" + (entry.ai_text || "").trim().toLowerCase();
    return (
      "manual:" +
      [entry.origin, entry.destination, entry.depart_date, entry.return_date, entry.trip_type]
        .map(function (v) { return (v || "").toString().trim().toLowerCase(); })
        .join("|")
    );
  }

  function pushRecent(entry) {
    var list = readRecent();
    var k = keyFor(entry);
    list = list.filter(function (item) { return keyFor(item) !== k; });
    list.unshift(entry);
    writeRecent(list);
    renderRecent();
  }

  function formatDateShort(iso) {
    if (!ISO_RE.test(iso || "")) return "";
    var parts = iso.split("-").map(Number);
    var d = new Date(parts[0], parts[1] - 1, parts[2]);
    if (Number.isNaN(d.getTime())) return "";
    return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(d);
  }

  function describeEntry(entry) {
    if (entry.mode === "ai") {
      return { title: entry.ai_text, sub: "AI search", kind: "AI search", icon: AI_ICON_SVG };
    }
    var route = (entry.origin || "?") + " → " + (entry.destination || "?");
    var dep = formatDateShort(entry.depart_date);
    var ret = formatDateShort(entry.return_date);
    var dateStr = entry.trip_type === "oneway" || !ret ? dep : dep + "–" + ret;
    var paxNum = parseInt(entry.passengers, 10) || 1;
    var pax = paxNum + (paxNum === 1 ? " traveler" : " travelers");
    var cabin = (entry.cabin || "").toLowerCase();
    cabin = cabin ? cabin.charAt(0).toUpperCase() + cabin.slice(1) : "";
    return { title: route, sub: [dateStr, pax, cabin].filter(Boolean).join(" · "), kind: "Flight", icon: ROUTE_ICON_SVG };
  }

  function renderRecent() {
    var section = document.getElementById("recentSearchesSection");
    var list = document.getElementById("recentSearchesList");
    if (!section || !list) return;
    var clear = document.getElementById("recentSearchesClear");
    var entries = readRecent();
    if (!entries.length) {
      section.hidden = true;
      if (clear) clear.hidden = true;
      return;
    }
    section.hidden = false;
    if (clear) clear.hidden = false;
    list.innerHTML = "";
    entries.forEach(function (entry) {
      var info = describeEntry(entry);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "home-recent-card";
      btn.setAttribute("role", "listitem");
      btn.setAttribute("aria-label", "Search again: " + info.title + (info.sub ? ", " + info.sub : ""));

      var icon = document.createElement("span");
      icon.className = "home-recent-icon";
      var iconImage = document.createElement("img");
      iconImage.src = info.icon;
      iconImage.alt = "";
      iconImage.setAttribute("aria-hidden", "true");
      icon.appendChild(iconImage);

      var text = document.createElement("span");
      text.className = "home-recent-text";
      var kind = document.createElement("span");
      kind.className = "home-recent-kind";
      kind.textContent = info.kind;
      var title = document.createElement("span");
      title.className = "home-recent-title";
      title.textContent = info.title;
      var sub = document.createElement("span");
      sub.className = "home-recent-sub";
      sub.textContent = info.sub;
      text.appendChild(kind);
      text.appendChild(title);
      text.appendChild(sub);

      var replay = document.createElement("span");
      replay.className = "home-recent-replay";
      replay.setAttribute("aria-hidden", "true");
      var replayImage = document.createElement("img");
      replayImage.src = REPLAY_ICON_SVG;
      replayImage.alt = "";
      replay.appendChild(replayImage);

      btn.appendChild(icon);
      btn.appendChild(text);
      btn.appendChild(replay);
      btn.addEventListener("click", function () { replaySearch(entry); });
      list.appendChild(btn);
    });

    updateRecentOverflowHint();
  }

  function updateRecentOverflowHint() {
    var section = document.getElementById("recentSearchesSection");
    var list = document.getElementById("recentSearchesList");
    if (!section || !list || section.hidden) return;
    var atEnd = list.scrollLeft + list.clientWidth >= list.scrollWidth - 4;
    section.classList.toggle("has-overflow", !atEnd);
    section.classList.toggle("has-overflow-start", list.scrollLeft > 4);
  }

  function replaySearch(entry) {
    if (entry.mode === "ai") {
      var aiForm = document.getElementById("aiForm");
      var aiText = document.getElementById("aiText");
      if (!aiForm || !aiText) return;
      aiText.value = entry.ai_text || "";
      aiForm.submit();
      return;
    }

    var mf = document.getElementById("unifiedForm");
    if (!mf) return;

    function setVal(name, value) {
      var el = mf.querySelector('[name="' + name + '"]');
      if (el && value != null) el.value = value;
    }

    var tripSel = mf.querySelector('[name="trip_type"]');
    if (tripSel) {
      tripSel.value = entry.trip_type || "roundtrip";
      tripSel.dispatchEvent(new Event("change", { bubbles: true }));
    }

    setVal("origin", entry.origin || "");
    setVal("destination", entry.destination || "");
    setVal("passengers", entry.passengers || "1");
    setVal("cabin", entry.cabin || "ECONOMY");

    var departHidden = document.getElementById("departPicker");
    var departDisplay = document.getElementById("departPickerDisplay");
    var returnHidden = document.getElementById("returnPicker");
    var returnDisplay = document.getElementById("returnPickerDisplay");

    function formatDisplayFull(iso) {
      var parts = iso.split("-").map(Number);
      var d = new Date(parts[0], parts[1] - 1, parts[2]);
      return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(d);
    }

    function setDateField(hiddenEl, displayEl, iso) {
      if (!hiddenEl) return;
      var next = ISO_RE.test(iso || "") ? iso : "";
      hiddenEl.value = next;
      hiddenEl.dispatchEvent(new Event("change", { bubbles: true }));
      if (displayEl) displayEl.value = next ? formatDisplayFull(next) : "";
    }

    setDateField(departHidden, departDisplay, entry.depart_date);
    if (entry.trip_type !== "oneway") {
      setDateField(returnHidden, returnDisplay, entry.return_date);
    }

    var nonstopEl = mf.querySelector('[name="nonstop"]');
    if (nonstopEl) nonstopEl.checked = entry.nonstop === "on";

    mf.submit();
  }

  function recordSubmissions() {
    document.addEventListener(
      "submit",
      function (e) {
        var target = e.target;
        if (!target || !target.id) return;

        if (target.id === "aiForm") {
          var aiText = document.getElementById("aiText");
          var txt = ((aiText && aiText.value) || "").trim();
          if (txt) pushRecent({ mode: "ai", ai_text: txt, ts: Date.now() });
          return;
        }

        if (target.id === "unifiedForm") {
          var get = function (name) {
            var el = target.querySelector('[name="' + name + '"]');
            return el ? (el.value || "").trim() : "";
          };
          var tripType = get("trip_type");
          var origin = get("origin");
          var destination = get("destination");
          if (tripType === "multicity" || !origin || !destination) return;
          pushRecent({
            mode: "manual",
            origin: origin,
            destination: destination,
            depart_date: get("depart_date"),
            return_date: get("return_date"),
            trip_type: tripType || "roundtrip",
            passengers: get("passengers") || "1",
            cabin: get("cabin") || "ECONOMY",
            nonstop: (target.querySelector('[name="nonstop"]') || {}).checked ? "on" : "",
            ts: Date.now(),
          });
        }
      },
      true
    );
  }

  function bindRecentClear() {
    var clear = document.getElementById("recentSearchesClear");
    if (!clear) return;
    clear.addEventListener("click", function () {
      try { localStorage.removeItem(RECENT_KEY); } catch (e) {}
      renderRecent();
    });
  }

  function readJsonScript(id) {
    var node = document.getElementById(id);
    if (!node) return [];
    try {
      var list = JSON.parse(node.textContent || "[]");
      return Array.isArray(list) ? list : [];
    } catch (e) {
      return [];
    }
  }

  function prefillDestinationSearch(city, opts) {
    opts = opts || {};
    var mf = document.getElementById("unifiedForm");
    var destInput = mf && mf.querySelector('[name="destination"]');
    if (destInput) {
      destInput.value = city;
      destInput.dispatchEvent(new Event("input", { bubbles: true }));
      destInput.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (typeof window.openManualSearch === "function") {
      window.openManualSearch("standard", { focus: false, scroll: true });
    }
    var compactQuery = window.matchMedia("(max-width: 760px)");
    window.setTimeout(
      function () {
        var originInput = mf && mf.querySelector('[name="origin"]');
        if (originInput && !originInput.value.trim()) {
          try { originInput.focus({ preventScroll: true }); } catch (e) { originInput.focus(); }
        }
      },
      compactQuery.matches ? (opts.scrollDelay || 700) : (opts.scrollDelay ? opts.scrollDelay - 300 : 400)
    );
  }

  /* Arriving from a destination landing page's "Search flights" link
     (e.g. /?dest=TYO): prefill + open the form, then clean the URL. */
  function applyDestQueryHandoff(destinationsByCode) {
    var params;
    try {
      params = new URLSearchParams(window.location.search);
    } catch (e) {
      return;
    }
    var code = (params.get("dest") || "").toUpperCase();
    if (!code) return;
    var dest = destinationsByCode[code];
    prefillDestinationSearch(dest ? dest.city : code);

    try {
      var url = new URL(window.location.href);
      url.searchParams.delete("dest");
      window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    } catch (e) {}
  }

  function formatMoney(amount, currency) {
    try {
      return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: currency || "USD",
        maximumFractionDigits: 0,
      }).format(amount);
    } catch (e) {
      return "$" + amount;
    }
  }

  function formatWeekendLabel(departIso, returnIso) {
    if (!ISO_RE.test(departIso || "") || !ISO_RE.test(returnIso || "")) return "";
    function short(iso) {
      var parts = iso.split("-").map(Number);
      var d = new Date(parts[0], parts[1] - 1, parts[2]);
      return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(d);
    }
    return short(departIso) + " – " + short(returnIso);
  }

  /* Submits the real, visible manual-search form so the date-picker/trip-type
     JS state stays in sync — takes the user straight to live results for the
     exact weekend a destination card's price was quoted for. */
  function goToLiveResults(origin, destinationCity, departIso, returnIso) {
    var mf = document.getElementById("unifiedForm");
    if (!mf) return;

    var tripSel = mf.querySelector('[name="trip_type"]');
    if (tripSel) {
      tripSel.value = "roundtrip";
      tripSel.dispatchEvent(new Event("change", { bubbles: true }));
    }

    var originInput = mf.querySelector('[name="origin"]');
    var destInput = mf.querySelector('[name="destination"]');
    if (originInput) originInput.value = origin || "JFK";
    if (destInput) destInput.value = destinationCity;

    var departHidden = document.getElementById("departPicker");
    var departDisplay = document.getElementById("departPickerDisplay");
    var returnHidden = document.getElementById("returnPicker");
    var returnDisplay = document.getElementById("returnPickerDisplay");

    function displayFormat(iso) {
      var parts = iso.split("-").map(Number);
      var d = new Date(parts[0], parts[1] - 1, parts[2]);
      return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(d);
    }

    if (departHidden) {
      departHidden.value = departIso;
      departHidden.dispatchEvent(new Event("change", { bubbles: true }));
      if (departDisplay) departDisplay.value = displayFormat(departIso);
    }
    if (returnHidden) {
      returnHidden.value = returnIso;
      returnHidden.dispatchEvent(new Event("change", { bubbles: true }));
      if (returnDisplay) returnDisplay.value = displayFormat(returnIso);
    }

    mf.submit();
  }

  /* "Popular flights near you": real device geolocation -> nearest major
     airport (falls back to New York/JFK if denied, unavailable, or slow) ->
     one live-pricing lookup covering both tabs' routes, so switching tabs is
     instant with no refetch. A route whose live fare lookup fails still
     shows its (real) photo/city/dates, just without a price badge — the
     rest of the app's "never fabricate a number" rule, applied here too. */
  var POPULAR_FLIGHTS_DEFAULT_ORIGIN = { code: "JFK", city: "New York" };
  var POPULAR_FLIGHTS_CARD_COUNT = 4;

  function initPopularFlights() {
    var grid = document.getElementById("popularFlightsGrid");
    var tabsWrap = document.querySelector(".home-flights-tabs");
    var sub = document.getElementById("popularFlightsSub");
    var note = document.getElementById("popularFlightsNote");
    if (!grid || !tabsWrap) return;

    var allDestinations = {
      international: readJsonScript("destinationsData"),
      domestic: readJsonScript("domesticDestinationsData"),
    };
    var byScope = { international: [], domestic: [] };
    var activeScope = "international";
    var resolved = null; // { originCity, originCode, prices: { CODE: {price, currency, depart_date, return_date} } }

    // Each destination is independently optimized (see /api/popular-flights
    // and _smart_destination_date_candidates on the backend): a short
    // weekend, a midweek trip, and a long weekend are all priced for real,
    // and whichever is genuinely cheapest for that specific route wins — so
    // cards routinely show different dates from each other, not one shared
    // weekend applied to everything.
    function buildFlightCard(dest, priceInfo) {
      var dateLabel = formatWeekendLabel(priceInfo.depart_date, priceInfo.return_date);
      var card = document.createElement("button");
      card.type = "button";
      card.className = "home-flight-card";
      card.setAttribute("aria-label", resolved.originCity + " to " + dest.city + ", from " + formatMoney(priceInfo.price, priceInfo.currency));

      var photo = document.createElement("span");
      photo.className = "home-flight-photo";
      var img = document.createElement("img");
      img.className = "home-flight-img";
      img.src = "/static/img/destinations/" + dest.photo;
      img.alt = dest.alt || dest.city;
      img.loading = "lazy";
      photo.appendChild(img);
      var deal = document.createElement("span");
      deal.className = "home-deal-badge";
      deal.textContent = "Members save 10%";
      photo.appendChild(deal);
      card.appendChild(photo);

      var body = document.createElement("span");
      body.className = "home-flight-body";
      var route = document.createElement("span");
      route.className = "home-flight-route";
      route.textContent = resolved.originCity + " to " + dest.city;
      var meta = document.createElement("span");
      meta.className = "home-flight-meta";
      meta.textContent = dateLabel ? dateLabel + " · Round-trip" : "Round-trip";
      body.appendChild(route);
      body.appendChild(meta);

      /* Expedia-style price block: struck public fare, bold Member Price.
         The live fare IS the member price; the compare-at is the same fare
         before the flat 10% Rewards discount is applied. */
      var priceRow = document.createElement("span");
      priceRow.className = "home-flight-price-row";
      var strike = document.createElement("s");
      strike.className = "home-flight-strike";
      strike.textContent = formatMoney(priceInfo.price / 0.9, priceInfo.currency);
      var price = document.createElement("span");
      price.className = "home-flight-price";
      price.textContent = formatMoney(priceInfo.price, priceInfo.currency);
      var tag = document.createElement("span");
      tag.className = "home-member-tag";
      tag.textContent = "Member Price";
      priceRow.appendChild(strike);
      priceRow.appendChild(price);
      priceRow.appendChild(tag);
      body.appendChild(priceRow);
      var priceNote = document.createElement("span");
      priceNote.className = "home-flight-price-note";
      priceNote.textContent = "Round-trip per traveler";
      body.appendChild(priceNote);
      card.appendChild(body);

      card.addEventListener("click", function () {
        goToLiveResults(resolved.originCode, dest.city, priceInfo.depart_date, priceInfo.return_date);
      });
      return card;
    }

    function renderScope(scope) {
      if (!resolved) return;
      grid.setAttribute("aria-busy", "false");
      grid.innerHTML = "";
      (byScope[scope] || []).forEach(function (dest) {
        // A destination with no verified fare across any candidate trip
        // has no real dates to show — omit it rather than guess.
        var priceInfo = resolved.prices[dest.code];
        if (priceInfo) grid.appendChild(buildFlightCard(dest, priceInfo));
      });
      note.hidden = !!grid.children.length;
      if (!grid.children.length) note.textContent = "No live flights found for this scope right now.";
    }

    tabsWrap.querySelectorAll(".home-flights-tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabsWrap.querySelectorAll(".home-flights-tab").forEach(function (t) {
          t.classList.remove("is-active");
          t.setAttribute("aria-selected", "false");
        });
        tab.classList.add("is-active");
        tab.setAttribute("aria-selected", "true");
        activeScope = tab.getAttribute("data-scope") || "international";
        renderScope(activeScope);
      });
    });

    function resolveOrigin() {
      // Page-load context, no user gesture — SkairGeo only resolves this
      // silently (cached fix, or permission the browser already granted).
      // It never pops a fresh permission prompt on its own; that's reserved
      // for the explicit "Use my location" buttons elsewhere on the page.
      if (!window.SkairGeo) return Promise.resolve(POPULAR_FLIGHTS_DEFAULT_ORIGIN);
      return window.SkairGeo.requestLocation({ auto: true, timeout: 5000 }).then(function (coords) {
        if (!coords) return POPULAR_FLIGHTS_DEFAULT_ORIGIN;
        return fetch("/api/nearest-airport", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lat: coords.lat, lng: coords.lng }),
        })
          .then(function (res) { return res.ok ? res.json() : null; })
          .then(function (data) { return data && data.code ? data : POPULAR_FLIGHTS_DEFAULT_ORIGIN; })
          .catch(function () { return POPULAR_FLIGHTS_DEFAULT_ORIGIN; });
      });
    }

    resolveOrigin().then(function (origin) {
      // A route to itself ("Los Angeles to Los Angeles") can happen whenever
      // the detected origin is also one of the curated domestic cities —
      // drop that one entry rather than showing a nonsense card.
      var originCityNorm = (origin.city || "").trim().toLowerCase();
      function excludesOrigin(d) {
        return d.code !== origin.code && d.city.trim().toLowerCase() !== originCityNorm;
      }
      byScope.international = allDestinations.international.filter(excludesOrigin).slice(0, POPULAR_FLIGHTS_CARD_COUNT);
      byScope.domestic = allDestinations.domestic.filter(excludesOrigin).slice(0, POPULAR_FLIGHTS_CARD_COUNT);

      var codes = [];
      byScope.international.concat(byScope.domestic).forEach(function (d) {
        if (codes.indexOf(d.code) === -1) codes.push(d.code);
      });
      if (!codes.length) return;

      fetch("/api/popular-flights", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ origin: origin.code, destinations: codes }),
      })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) {
          if (!data) throw new Error("popular flights lookup failed");
          resolved = {
            originCity: origin.city,
            originCode: origin.code,
            prices: data.prices || {},
          };
          if (sub) sub.textContent = "Find deals on domestic and international flights from " + origin.city;
          renderScope(activeScope);
        })
        .catch(function () {
          grid.setAttribute("aria-busy", "false");
          grid.innerHTML = "";
          note.textContent = "Live flight deals aren't available right now — try again shortly.";
          note.hidden = false;
        });
    });
  }

  var destinationsByCode = {};
  readJsonScript("destinationsData").forEach(function (d) {
    destinationsByCode[d.code] = d;
  });

  bindRecentClear();
  recordSubmissions();
  renderRecent();
  initPopularFlights();
  applyDestQueryHandoff(destinationsByCode);

  window.addEventListener("resize", updateRecentOverflowHint);
  var recentScrollEl = document.getElementById("recentSearchesList");
  if (recentScrollEl) {
    recentScrollEl.addEventListener("scroll", updateRecentOverflowHint, { passive: true });
  }
})();
