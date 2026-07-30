/*
 * Homepage discovery sections:
 * - "Recent searches": localStorage-backed, functional replay of real past
 *   searches (also doubles as the "light personalization" signal below).
 * - "Popular destinations": real photos + editorial content, travel-intent
 *   category tabs, a live "from $X" fare pulled from the real search
 *   backend (never fabricated — omitted if the lookup fails), and an
 *   editorial two-destination comparison tool.
 */
(function () {
  "use strict";

  var RECENT_KEY = "skair_recent_searches";
  var MAX_RECENT = 6;
  var ISO_RE = /^\d{4}-\d{2}-\d{2}$/;

  var AI_ICON_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 3v3M12 18v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M3 12h3M18 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>' +
    "</svg>";
  var ROUTE_ICON_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M2 16.5 22 8l-5.5 13-2.7-6.3L8 18.2z"/><path d="M13.8 14.7 22 8"/>' +
    "</svg>";

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
      return { title: entry.ai_text, sub: "AI search", icon: AI_ICON_SVG };
    }
    var route = (entry.origin || "?") + " → " + (entry.destination || "?");
    var dep = formatDateShort(entry.depart_date);
    var ret = formatDateShort(entry.return_date);
    var dateStr = entry.trip_type === "oneway" || !ret ? dep : dep + "–" + ret;
    var paxNum = parseInt(entry.passengers, 10) || 1;
    var pax = paxNum + (paxNum === 1 ? " traveler" : " travelers");
    return { title: route, sub: [dateStr, pax].filter(Boolean).join(" · "), icon: ROUTE_ICON_SVG };
  }

  function renderRecent() {
    var section = document.getElementById("recentSearchesSection");
    var list = document.getElementById("recentSearchesList");
    if (!section || !list) return;
    var entries = readRecent();
    if (!entries.length) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
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
      icon.innerHTML = info.icon;

      var text = document.createElement("span");
      text.className = "home-recent-text";
      var title = document.createElement("span");
      title.className = "home-recent-title";
      title.textContent = info.title;
      var sub = document.createElement("span");
      sub.className = "home-recent-sub";
      sub.textContent = info.sub;
      text.appendChild(title);
      text.appendChild(sub);

      btn.appendChild(icon);
      btn.appendChild(text);
      btn.addEventListener("click", function () { replaySearch(entry); });
      list.appendChild(btn);
    });

    updateRecentOverflowHint();
  }

  function updateRecentOverflowHint() {
    var section = document.getElementById("recentSearchesSection");
    var list = document.getElementById("recentSearchesList");
    if (!section || !list || section.hidden) return;
    var hasOverflow = list.scrollWidth - list.clientWidth > 4;
    section.classList.toggle("has-overflow", hasOverflow);
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

  function readDestinationsData() {
    var node = document.getElementById("destinationsData");
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

  function updateDestinationOverflowHint() {
    var grid = document.getElementById("popularDestinationsGrid");
    if (!grid || grid.classList.contains("is-expanded")) {
      if (grid) grid.classList.remove("has-overflow");
      return;
    }
    var hasOverflow = grid.scrollWidth - grid.clientWidth > 4;
    grid.classList.toggle("has-overflow", hasOverflow);
  }

  function initDestinationTabs() {
    var tabsWrap = document.querySelector(".home-destination-tabs");
    var grid = document.getElementById("popularDestinationsGrid");
    if (!tabsWrap || !grid) return;
    var tabs = Array.prototype.slice.call(tabsWrap.querySelectorAll(".home-destination-tab"));
    var cards = Array.prototype.slice.call(grid.querySelectorAll(".home-destination-card"));

    function applyFilter(categorySlug) {
      cards.forEach(function (card) {
        var cats = (card.getAttribute("data-dest-categories") || "").split(",").filter(Boolean);
        card.hidden = !(!categorySlug || cats.indexOf(categorySlug) !== -1);
      });
      grid.scrollLeft = 0;
      window.setTimeout(updateDestinationOverflowHint, 50);
    }

    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t) {
          t.classList.remove("is-active");
          t.setAttribute("aria-selected", "false");
        });
        tab.classList.add("is-active");
        tab.setAttribute("aria-selected", "true");
        applyFilter(tab.getAttribute("data-category") || "");
      });
    });

    grid.addEventListener(
      "scroll",
      function () {
        if (grid.classList.contains("is-expanded")) return;
        var atEnd = grid.scrollLeft + grid.clientWidth >= grid.scrollWidth - 4;
        grid.classList.toggle("has-overflow", !atEnd);
      },
      { passive: true }
    );

    window.addEventListener("resize", updateDestinationOverflowHint);
    window.setTimeout(updateDestinationOverflowHint, 300);
  }

  function initDestinationViewAllToggle() {
    var toggle = document.getElementById("destinationViewAllToggle");
    var grid = document.getElementById("popularDestinationsGrid");
    if (!toggle || !grid) return;

    toggle.addEventListener("click", function () {
      var expanded = grid.classList.toggle("is-expanded");
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      toggle.querySelector(".home-destination-viewall-label").textContent = expanded ? "Show fewer" : "View all";
      if (expanded) {
        grid.classList.remove("has-overflow");
      } else {
        window.setTimeout(updateDestinationOverflowHint, 50);
      }
    });
  }

  var COMPARE_MAX = 2;

  function initCompareTool(destinationsBySlug) {
    var grid = document.getElementById("popularDestinationsGrid");
    var bar = document.getElementById("compareBar");
    var barLabel = document.getElementById("compareBarLabel");
    var barCta = document.getElementById("compareBarCta");
    var barClear = document.getElementById("compareBarClear");
    var modal = document.getElementById("compareModal");
    var modalBody = document.getElementById("compareModalBody");
    var modalClose = document.getElementById("compareModalClose");
    if (!grid || !bar || !modal) return;

    var selection = [];

    function toggleBtnState(slug, active) {
      var btn = grid.querySelector('[data-compare-toggle="' + slug + '"]');
      if (!btn) return;
      btn.setAttribute("aria-pressed", active ? "true" : "false");
      btn.classList.toggle("is-active", active);
    }

    function updateBar() {
      if (!selection.length) {
        bar.hidden = true;
        return;
      }
      bar.hidden = false;
      var names = selection.map(function (slug) {
        var d = destinationsBySlug[slug];
        return d ? d.city : slug;
      });
      barLabel.textContent =
        selection.length === 1
          ? "Comparing: " + names[0] + " — pick one more"
          : "Comparing: " + names.join(" vs ");
      barCta.disabled = selection.length !== COMPARE_MAX;
    }

    function toggleSlug(slug) {
      var idx = selection.indexOf(slug);
      if (idx !== -1) {
        selection.splice(idx, 1);
        toggleBtnState(slug, false);
      } else {
        if (selection.length >= COMPARE_MAX) {
          var removed = selection.shift();
          toggleBtnState(removed, false);
        }
        selection.push(slug);
        toggleBtnState(slug, true);
      }
      updateBar();
    }

    grid.querySelectorAll("[data-compare-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        toggleSlug(btn.getAttribute("data-compare-toggle"));
      });
    });

    barClear.addEventListener("click", function () {
      selection.slice().forEach(function (slug) { toggleBtnState(slug, false); });
      selection = [];
      updateBar();
    });

    var TRAIT_LABELS = {
      food: "Food",
      nightlife: "Nightlife",
      culture: "Culture & history",
      budget: "Budget-friendly",
      nature: "Nature & beaches",
      shopping: "Shopping",
    };

    function stars(n) {
      var full = Math.max(0, Math.min(5, n || 0));
      return "★★★★★".slice(0, full) + "☆☆☆☆☆".slice(0, 5 - full);
    }

    function renderComparison() {
      if (selection.length !== COMPARE_MAX) return;
      var a = destinationsBySlug[selection[0]];
      var b = destinationsBySlug[selection[1]];
      if (!a || !b) return;

      var html = "";
      html += '<p class="home-compare-editorial-note">Our take — an editorial comparison to help you decide, not measured data.</p>';
      html += '<div class="home-compare-heads">';
      [a, b].forEach(function (d) {
        html +=
          '<div class="home-compare-head">' +
          '<span class="home-compare-head-city">' + d.city + "</span>" +
          '<span class="home-compare-head-country">' + d.country + "</span>" +
          "</div>";
      });
      html += "</div>";

      html += '<div class="home-compare-rows">';
      Object.keys(TRAIT_LABELS).forEach(function (key) {
        html +=
          '<div class="home-compare-row">' +
          '<span class="home-compare-cell home-compare-cell--a">' + stars((a.traits || {})[key]) + "</span>" +
          '<span class="home-compare-cell-label">' + TRAIT_LABELS[key] + "</span>" +
          '<span class="home-compare-cell home-compare-cell--b">' + stars((b.traits || {})[key]) + "</span>" +
          "</div>";
      });
      html += "</div>";

      var aBudget = (a.traits || {}).budget || 0;
      var bBudget = (b.traits || {}).budget || 0;
      var cheaper = aBudget >= bBudget ? a : b;
      var pricier = cheaper === a ? b : a;
      var pricierStrength = (pricier.traits || {}).nightlife >= (pricier.traits || {}).culture ? "nightlife" : "culture and history";
      var verdict =
        cheaper.city + " is the easier choice on budget; " + pricier.city + " has the edge on " + pricierStrength + ".";
      html += '<p class="home-compare-verdict">' + verdict + "</p>";

      html +=
        '<div class="home-compare-cta-row">' +
        '<a class="home-compare-explore" href="/destinations/' + a.slug + '">Explore ' + a.city + "</a>" +
        '<a class="home-compare-explore" href="/destinations/' + b.slug + '">Explore ' + b.city + "</a>" +
        "</div>";

      modalBody.innerHTML = html;
    }

    barCta.addEventListener("click", function () {
      if (selection.length !== COMPARE_MAX) return;
      renderComparison();
      modal.hidden = false;
      document.body.classList.add("home-compare-open");
    });

    function closeModal() {
      modal.hidden = true;
      document.body.classList.remove("home-compare-open");
    }
    modalClose.addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) {
      if (e.target === modal) closeModal();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !modal.hidden) closeModal();
    });
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
      return new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric" }).format(d);
    }
    return short(departIso) + " – " + short(returnIso);
  }

  function getPersonalizedOrigin() {
    var entries = readRecent();
    for (var i = 0; i < entries.length; i += 1) {
      if (entries[i].mode === "manual" && entries[i].origin) return entries[i].origin;
    }
    return "";
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

  function initLivePricing() {
    var slots = Array.prototype.slice.call(document.querySelectorAll("[data-price-slot]"));
    if (!slots.length) return;

    slots.forEach(function (slot) {
      var skeleton = document.createElement("span");
      skeleton.className = "home-destination-price-skeleton";
      skeleton.setAttribute("aria-hidden", "true");
      slot.appendChild(skeleton);
    });

    var codes = slots.map(function (s) { return s.getAttribute("data-dest-code"); });
    var origin = getPersonalizedOrigin();

    fetch("/api/destination-prices", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin: origin, destinations: codes }),
    })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (!data) throw new Error("destination price lookup failed");
        var prices = data.prices || {};
        var dateLabel = formatWeekendLabel(data.depart_date, data.return_date);
        slots.forEach(function (slot) {
          var code = slot.getAttribute("data-dest-code");
          var city = slot.getAttribute("data-dest-city");
          var info = prices[code];
          if (info && info.price) {
            slot.textContent = formatMoney(info.price, info.currency) + " round trip";
            slot.classList.add("is-loaded");
            slot.disabled = false;
            slot.setAttribute("aria-label", "See live flight results to " + city + " for " + dateLabel);
            slot.addEventListener("click", function () {
              goToLiveResults(origin, city, data.depart_date, data.return_date);
            });
          } else {
            slot.remove();
          }
        });
      })
      .catch(function () {
        slots.forEach(function (slot) { slot.remove(); });
      });
  }

  var destinationsList = readDestinationsData();
  var destinationsBySlug = {};
  var destinationsByCode = {};
  destinationsList.forEach(function (d) {
    destinationsBySlug[d.slug] = d;
    destinationsByCode[d.code] = d;
  });

  recordSubmissions();
  renderRecent();
  initDestinationTabs();
  initDestinationViewAllToggle();
  initCompareTool(destinationsBySlug);
  initLivePricing();
  applyDestQueryHandoff(destinationsByCode);

  window.addEventListener("resize", updateRecentOverflowHint);
  var recentScrollEl = document.getElementById("recentSearchesList");
  if (recentScrollEl) {
    recentScrollEl.addEventListener(
      "scroll",
      function () {
        var section = document.getElementById("recentSearchesSection");
        if (!section) return;
        var atEnd = recentScrollEl.scrollLeft + recentScrollEl.clientWidth >= recentScrollEl.scrollWidth - 4;
        section.classList.toggle("has-overflow", !atEnd);
      },
      { passive: true }
    );
  }
})();
