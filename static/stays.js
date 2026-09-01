/* Shared behaviour for the Stays pages: destination autocomplete, date
   coupling, horizontal rails. Used by the landing hero, the results topbar,
   and the Stays tab on the home page — one implementation, three mounts. */
window.StaysSearch = (function () {
  function byId(id) { return id ? document.getElementById(id) : null; }

  function init(opts) {
    var input = byId(opts.input);
    var placeId = byId(opts.placeId);
    var list = byId(opts.list);
    var checkin = byId(opts.checkin);
    var checkout = byId(opts.checkout);
    var hint = byId(opts.hint);
    var form = byId(opts.form);
    if (!input || !placeId || !list) return;

    var itemClass = opts.itemClass || "nu-suggest-item";
    var nameClass = opts.nameClass || "nu-suggest-name";
    var addrClass = opts.addrClass || "nu-suggest-addr";
    var timer = null, items = [], active = -1;
    var lookupController = null;
    var lookupSequence = 0;
    var lookupKey = "";
    var placeCache = Object.create(null);
    var placeCacheOrder = [];
    var PLACE_CACHE_TTL = 120000;
    var PLACE_CACHE_LIMIT = 16;

    function iso(d) { return d.toISOString().slice(0, 10); }

    if (checkin && checkout) {
      var today = iso(new Date());
      checkin.min = today;
      checkout.min = today;
      // Seed empty pickers 30 days out for a 3-night stay.
      if (!checkin.value) {
        var s = new Date(); s.setDate(s.getDate() + 30);
        var e = new Date(s); e.setDate(e.getDate() + 3);
        checkin.value = iso(s); checkout.value = iso(e);
        // Date fields may be rendered by the shared range calendar, which
        // retains these ISO inputs for submission and mirrors updates through
        // normal input/change events.
        checkin.dispatchEvent(new Event("input", { bubbles: true }));
        checkout.dispatchEvent(new Event("input", { bubbles: true }));
      }
      checkin.addEventListener("change", function () {
        var d = new Date(checkin.value); d.setDate(d.getDate() + 1);
        checkout.min = iso(d);
        if (!checkout.value || checkout.value <= checkin.value) checkout.value = iso(d);
      });
    }

    var suggestCloseTimer = null;
    function reducedMotion() {
      return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    }

    function hide(immediate) {
      if (suggestCloseTimer) { window.clearTimeout(suggestCloseTimer); suggestCloseTimer = null; }
      input.setAttribute("aria-expanded", "false"); active = -1;
      list.classList.remove("is-open");
      var finish = function () {
        if (list.classList.contains("is-open")) return;
        list.hidden = true;
        list.innerHTML = "";
      };
      if (immediate || list.hidden || reducedMotion()) finish();
      else suggestCloseTimer = window.setTimeout(finish, 160);
    }

    function cancelLookup() {
      lookupSequence += 1;
      lookupKey = "";
      if (lookupController) {
        try { lookupController.abort(); } catch (e) {}
        lookupController = null;
      }
    }

    function close() {
      if (timer) { clearTimeout(timer); timer = null; }
      cancelLookup();
      hide();
    }

    function normaliseQuery(value) {
      return (value || "").trim().replace(/\s+/g, " ").toLowerCase();
    }

    function cacheRows(key, rows) {
      placeCache[key] = { rows: rows, expiresAt: Date.now() + PLACE_CACHE_TTL };
      placeCacheOrder = placeCacheOrder.filter(function (cachedKey) { return cachedKey !== key; });
      placeCacheOrder.push(key);
      while (placeCacheOrder.length > PLACE_CACHE_LIMIT) {
        delete placeCache[placeCacheOrder.shift()];
      }
    }

    function cachedRows(key) {
      var cached = placeCache[key];
      if (!cached) return null;
      if (cached.expiresAt < Date.now()) {
        delete placeCache[key];
        placeCacheOrder = placeCacheOrder.filter(function (cachedKey) { return cachedKey !== key; });
        return null;
      }
      return cached.rows;
    }

    function choose(it) {
      input.value = it.name;
      placeId.value = it.place_id;
      if (hint) { hint.textContent = it.address || it.name; hint.classList.remove("is-error"); }
      close();
    }

    function render(rows) {
      if (suggestCloseTimer) { window.clearTimeout(suggestCloseTimer); suggestCloseTimer = null; }
      items = rows; list.innerHTML = "";
      if (!rows.length) return hide();
      rows.forEach(function (it, i) {
        var li = document.createElement("li");
        li.className = itemClass;
        li.setAttribute("role", "option");
        li.dataset.i = i;
        var n = document.createElement("span"); n.className = nameClass; n.textContent = it.name;
        var a = document.createElement("span"); a.className = addrClass; a.textContent = it.address || "";
        li.appendChild(n); li.appendChild(a);
        li.addEventListener("mousedown", function (e) { e.preventDefault(); choose(it); });
        list.appendChild(li);
      });
      list.hidden = false;
      window.requestAnimationFrame(function () { list.classList.add("is-open"); });
      input.setAttribute("aria-expanded", "true");
    }

    function lookup(q) {
      q = (q || "").trim();
      var key = normaliseQuery(q);
      if (key.length < 2) return close();

      // A repeated input event for the same text should never create a second
      // in-flight request. Reuse a short-lived response while the visitor is
      // still choosing a place instead.
      if (lookupController && lookupKey === key) return;
      var cached = cachedRows(key);
      if (cached) {
        if (normaliseQuery(input.value) === key) render(cached);
        return;
      }

      cancelLookup();
      lookupKey = key;
      var sequence = ++lookupSequence;
      var controller = typeof window.AbortController === "function" ? new window.AbortController() : null;
      lookupController = controller;
      var requestOptions = controller ? { signal: controller.signal } : undefined;

      fetch("/api/hotels/places?q=" + encodeURIComponent(q), requestOptions)
        .then(function (r) {
          if (!r.ok) throw new Error("Unable to load place suggestions");
          return r.json();
        })
        .then(function (rows) {
          if (sequence !== lookupSequence || normaliseQuery(input.value) !== key) return;
          lookupController = null;
          lookupKey = "";
          cacheRows(key, Array.isArray(rows) ? rows : []);
          render(Array.isArray(rows) ? rows : []);
        })
        .catch(function (error) {
          // An aborted request was superseded by a newer query. It must not
          // close that newer list or overwrite its result.
          if (sequence !== lookupSequence || (error && error.name === "AbortError")) return;
          lookupController = null;
          lookupKey = "";
          hide();
        });
    }

    input.addEventListener("input", function () {
      placeId.value = "";
      if (timer) { clearTimeout(timer); timer = null; }
      cancelLookup();
      var q = input.value.trim();
      if (q.length < 2) return close();
      timer = setTimeout(function () { lookup(q); }, 220);
    });

    input.addEventListener("keydown", function (e) {
      if (list.hidden) return;
      var els = list.querySelectorAll("." + itemClass);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active += e.key === "ArrowDown" ? 1 : -1;
        if (active < 0) active = els.length - 1;
        if (active >= els.length) active = 0;
        els.forEach(function (el, i) { el.classList.toggle("is-active", i === active); });
      } else if (e.key === "Enter" && active >= 0) {
        e.preventDefault(); choose(items[active]);
      } else if (e.key === "Escape") { close(); }
    });

    input.addEventListener("blur", function () { setTimeout(close, 120); });
    window.addEventListener("pagehide", cancelLookup, { once: true });

    // A placeId is required upstream — a free-typed city alone will not resolve.
    if (form) {
      form.addEventListener("submit", function (e) {
        if (!placeId.value) {
          e.preventDefault();
          if (hint) { hint.textContent = "Please pick a destination from the suggestions."; hint.classList.add("is-error"); }
          input.focus();
        }
      });
    }

    // Destination shortcuts (popular-city cards / chips)
    document.querySelectorAll("[data-city]").forEach(function (el) {
      el.addEventListener("click", function () {
        input.value = el.dataset.city;
        input.focus();
        lookup(el.dataset.city);
      });
    });
  }

  /* ---------------- manual <-> AI mode switch ----------------
     Swaps which form is mounted in the hero pill. The chosen mode sticks
     across visits, and context carries over: switching to AI seeds the
     prompt from whatever the manual fields already hold. */
  var MODE_KEY = "skair_stays_mode";

  function readMode() {
    try { return localStorage.getItem(MODE_KEY) === "ai" ? "ai" : "manual"; } catch (e) { return "manual"; }
  }

  function modeToggle(opts) {
    var manualForm = byId(opts.manualForm);
    var aiForm = byId(opts.aiForm);
    var aiText = byId(opts.aiText);
    if (!manualForm || !aiForm || !aiText) return;

    var chips = byId(opts.chips);
    var status = byId(opts.status);
    var hint = byId(opts.hint);
    var where = byId(opts.where);
    var checkin = byId(opts.checkin);
    var checkout = byId(opts.checkout);
    var tabs = (opts.tabs || "").split(",").map(function (id) { return byId(id.trim()); }).filter(Boolean);
    var modeEnterTimer = null;

    // Mirrors the flight prompt's autoResize() on the home page: once the
    // request actually needs more than one line, it gets its own row above
    // the icon/submit row (see .ai-reference-row.is-multiline in
    // ai-composer.css) instead of staying pinned to the single-line pill.
    var aiRow = aiText.closest(".ai-reference-row");
    function grow() {
      var width = window.innerWidth || document.documentElement.clientWidth || 0;
      aiText.style.whiteSpace = "pre-wrap";
      aiText.style.overflowWrap = "anywhere";
      aiText.style.wordBreak = "break-word";
      var lineHeight = parseFloat(window.getComputedStyle(aiText).lineHeight) || 24;
      var maxHeight = Math.ceil(lineHeight * (width <= 760 ? 2 : 4));
      aiText.style.height = "0px";
      var inlineHeight = aiText.scrollHeight;
      // Not an arbitrary character count — that ballooned the pill for
      // short single-line text that never came close to wrapping.
      var shouldExpand = inlineHeight > lineHeight * 1.4;
      if (aiRow) aiRow.classList.toggle("is-multiline", shouldExpand);
      var minHeight = Math.ceil(lineHeight * (shouldExpand ? 2 : 1));
      var next = Math.min(Math.max(aiText.scrollHeight, minHeight), maxHeight);
      aiText.style.height = next + "px";
      aiText.style.overflowY = aiText.scrollHeight > maxHeight + 1 ? "auto" : "hidden";
    }
    window.addEventListener("resize", grow);

    // scrollHeight ignores the placeholder, so a wrapping one would sit half
    // hidden in the single-row box. Narrow screens get the short version.
    var shortHint = aiText.dataset.placeholderSm;
    var longHint = aiText.placeholder;
    if (shortHint && window.matchMedia) {
      var narrow = window.matchMedia("(max-width: 620px)");
      var syncHint = function () { aiText.placeholder = narrow.matches ? shortHint : longHint; };
      syncHint();
      if (narrow.addEventListener) narrow.addEventListener("change", syncHint);
      else if (narrow.addListener) narrow.addListener(syncHint);
    }

    // Carry the manual destination/dates into the prompt so switching modes
    // never costs the user what they already typed. Never overwrites a draft.
    function seedFromManual() {
      if (aiText.value.trim()) return;
      var city = where && where.value.trim();
      if (!city) return;
      var text = "Hotel in " + city;
      if (checkin && checkout && checkin.value && checkout.value) {
        text += " from " + checkin.value + " to " + checkout.value;
      }
      aiText.value = text;
      grow();
    }

    function apply(mode, focus) {
      var ai = mode === "ai";
      var nextForm = ai ? aiForm : manualForm;
      var previousForm = ai ? manualForm : aiForm;
      var switching = Boolean(focus) && previousForm.hidden === false && nextForm.hidden === true;
      previousForm.hidden = true;
      nextForm.hidden = false;
      if (switching && !(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches)) {
        if (modeEnterTimer) window.clearTimeout(modeEnterTimer);
        nextForm.classList.remove("is-mode-entering");
        window.requestAnimationFrame(function () {
          nextForm.classList.add("is-mode-entering");
          modeEnterTimer = window.setTimeout(function () { nextForm.classList.remove("is-mode-entering"); }, 230);
        });
      }
      if (chips) chips.hidden = !ai;
      if (hint) hint.classList.remove("is-error");
      if (status) status.hidden = true;
      tabs.forEach(function (tab) {
        var on = (tab.dataset.mode === "ai") === ai;
        tab.classList.toggle("is-active", on);
        tab.setAttribute("aria-selected", on ? "true" : "false");
      });
      if (ai) seedFromManual();
      if (focus) (ai ? aiText : where || aiText).focus();
      try { localStorage.setItem(MODE_KEY, mode); } catch (e) {}
    }

    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () { apply(tab.dataset.mode, true); });
    });

    aiText.addEventListener("input", function () {
      grow();
      // Typing answers the "tell us where" prompt — drop the warning.
      if (hint && aiText.value.trim()) hint.classList.remove("is-error");
    });

    // Enter searches, Shift+Enter breaks the line — same contract as the
    // flight prompt on the home page.
    aiText.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (typeof aiForm.requestSubmit === "function") aiForm.requestSubmit();
        else aiForm.submit();
      }
    });

    if (chips) {
      chips.querySelectorAll("[data-prompt]").forEach(function (chip) {
        chip.addEventListener("click", function () {
          aiText.value = chip.dataset.prompt;
          grow();
          if (hint) hint.classList.remove("is-error");
          aiText.focus();
          aiText.setSelectionRange(aiText.value.length, aiText.value.length);
        });
      });
    }

    aiForm.addEventListener("submit", function (e) {
      if (!aiText.value.trim()) {
        e.preventDefault();
        if (hint) { hint.textContent = "Tell us where you want to stay and when."; hint.classList.add("is-error"); }
        aiText.focus();
        return;
      }
      // Parsing plus the rate search runs for a few seconds on a full page
      // load, so lock the button and explain the wait.
      if (hint) hint.classList.remove("is-error");
      aiForm.classList.add("is-busy");
      if (status) status.hidden = false;
    });

    // Coming back via the back button restores the page from bfcache mid-submit,
    // which would otherwise leave the button spinning forever.
    window.addEventListener("pageshow", function (e) {
      if (!e.persisted) return;
      aiForm.classList.remove("is-busy");
      if (status) status.hidden = true;
    });

    // Restore without stealing focus on load.
    apply(readMode(), false);
  }

  /* ---------------- property card rendering ---------------- */
  function money(n) {
    return "US$" + Math.round(Number(n) || 0).toLocaleString();
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function propertyCard(c, opts) {
    opts = opts || {};
    var a = el("a", "nu-card");
    a.href = c.url || "#";

    var media = el("span", "nu-card-img");
    if (c.photo) {
      var img = el("img");
      img.src = c.photo; img.alt = ""; img.loading = "lazy"; img.decoding = "async";
      // Some supplier records point at dead image URLs — fall back rather than
      // leaving a blank tile.
      img.addEventListener("error", function () {
        img.remove();
        media.classList.add("nu-card-img--empty");
      });
      media.appendChild(img);
    } else {
      media.classList.add("nu-card-img--empty");
    }
    a.appendChild(media);

    var body = el("span", "nu-card-body");
    if (c.stars) body.appendChild(el("span", "nu-stars", "★".repeat(c.stars)));
    body.appendChild(el("span", "nu-card-name", c.name));

    var loc = el("span", "nu-card-loc");
    loc.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11Z"/><circle cx="12" cy="10" r="2.6"/></svg>';
    loc.appendChild(el("span", null, [c.city, c.address].filter(Boolean).join(", ")));
    body.appendChild(loc);

    if (opts.distance && c.distance_miles != null) {
      body.appendChild(el("span", "nu-card-dist", c.distance_miles + " mi from center"));
    }

    var foot = el("span", "nu-card-foot");
    var score = el("span", "nu-score");
    if (c.rating) {
      var badge = el("span", "nu-score-badge" + (c.rating < 8 ? " nu-score-badge--mid" : ""), c.rating);
      score.appendChild(badge);
      var st = el("span", "nu-score-text");
      st.appendChild(el("strong", null, c.review_label || ""));
      if (c.review_count) st.appendChild(el("span", null, c.review_count.toLocaleString() + " reviews"));
      score.appendChild(st);
    }
    foot.appendChild(score);

    var price = el("span", "nu-price");
    if (c.offer && c.offer.was_amount) price.appendChild(el("s", null, money(c.offer.was_amount)));
    price.appendChild(el("strong", null, money(c.offer ? c.offer.total_amount : 0)));
    price.appendChild(el("span", null, "1 room x 1 night incl. taxes"));
    foot.appendChild(price);

    body.appendChild(foot);
    a.appendChild(body);
    return a;
  }

  var railRequests = Object.create(null);
  var showcaseStarted = false;

  function isAbort(error) {
    return Boolean(error && error.name === "AbortError");
  }

  function fillRail(railId, sectionId, url, opts) {
    var rail = document.getElementById(railId);
    var section = document.getElementById(sectionId);
    if (!rail) return;

    var state = railRequests[railId] || (railRequests[railId] = {
      controller: null, token: 0, url: "", pending: false, loaded: false,
    });
    // The Hotels landing page can be restored or mounted more than once. Do
    // not make another expensive hotel call when this rail already has the
    // same request in flight (or has already rendered it this visit).
    if (state.url === url && (state.pending || state.loaded)) return;

    if (state.controller) {
      try { state.controller.abort(); } catch (e) {}
    }
    state.url = url;
    state.pending = true;
    state.loaded = false;
    var token = ++state.token;
    var controller = typeof window.AbortController === "function" ? new window.AbortController() : null;
    state.controller = controller;
    var requestOptions = controller ? { signal: controller.signal } : undefined;

    fetch(url, requestOptions)
      .then(function (r) {
        if (!r.ok) throw new Error("Unable to load hotel rail");
        return r.json();
      })
      .then(function (rows) {
        if (state.token !== token || state.url !== url) return;
        state.controller = null;
        state.pending = false;
        state.loaded = true;
        rail.innerHTML = "";
        if (!rows || !rows.length) {
          if (section) section.hidden = true;
          return;
        }
        rows.forEach(function (c) { rail.appendChild(propertyCard(c, opts)); });
        if (section) section.hidden = false;
      })
      .catch(function (error) {
        if (state.token !== token || state.url !== url || isAbort(error)) return;
        state.controller = null;
        state.pending = false;
        state.loaded = false;
        if (section) section.hidden = true;
      });
  }

  function showcase() {
    if (showcaseStarted) return;
    showcaseStarted = true;
    fillRail("railRecommended", "recommendedSection", "/api/hotels/recommended");

    // Nearby needs consent; stay silent if the visitor declines or it fails.
    // Page-load context, no user gesture — SkairGeo only resolves this from
    // a cached fix or a permission the browser already granted, and never
    // pops a fresh prompt on its own.
    var note = document.getElementById("nearbyNote");
    if (!window.SkairGeo || !navigator.geolocation) return;
    window.SkairGeo.requestLocation({ auto: true, timeout: 8000 }).then(function (coords) {
      if (!coords) return; // declined, undecided, or unavailable — section stays hidden
      fillRail(
        "railNearby", "nearbySection",
        "/api/hotels/nearby?lat=" + coords.lat + "&lng=" + coords.lng,
        { distance: true }
      );
      if (note) { note.textContent = "Based on your current location."; note.hidden = false; }
    });
  }

  window.addEventListener("pagehide", function () {
    Object.keys(railRequests).forEach(function (railId) {
      var state = railRequests[railId];
      if (!state || !state.controller) return;
      try { state.controller.abort(); } catch (e) {}
      state.controller = null;
      state.pending = false;
    });
  }, { once: true });

  /* ---------------- recent stay searches ---------------- */
  var RECENT_KEY = "skair_recent_stays";
  var MAX_RECENT = 6;

  function readRecent() {
    try {
      var raw = localStorage.getItem(RECENT_KEY);
      var list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch (e) { return []; }
  }

  function pushRecent(entry) {
    if (!entry || !entry.place_id) return;
    var list = readRecent().filter(function (i) {
      return !(i.place_id === entry.place_id && i.checkin === entry.checkin && i.checkout === entry.checkout);
    });
    list.unshift(entry);
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, MAX_RECENT))); } catch (e) {}
  }

  function shortDate(iso) {
    var d = new Date(iso + "T00:00:00");
    if (isNaN(d)) return iso;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  // Edge shadows for the horizontal rail — mirrors the Flights home page's
  // recent-searches viewport (see .home-recent-viewport in home-sections.js).
  function updateRecentOverflowHint() {
    var section = document.getElementById("recentStaysSection");
    var list = document.getElementById("recentStaysList");
    if (!section || !list || section.hidden) return;
    var atEnd = list.scrollLeft + list.clientWidth >= list.scrollWidth - 4;
    section.classList.toggle("has-overflow", !atEnd);
    section.classList.toggle("has-overflow-start", list.scrollLeft > 4);
  }

  function renderRecent() {
    var section = document.getElementById("recentStaysSection");
    var list = document.getElementById("recentStaysList");
    if (!section || !list) return;
    var entries = readRecent();
    if (!entries.length) { section.hidden = true; return; }

    list.innerHTML = "";
    entries.forEach(function (entry) {
      var btn = el("button", "nu-recent-item");
      btn.type = "button";
      btn.setAttribute("role", "listitem");
      btn.appendChild(el("span", "nu-recent-city", entry.place_name));
      var nights = Math.max(1, Math.round(
        (new Date(entry.checkout) - new Date(entry.checkin)) / 86400000
      ));
      btn.appendChild(el("span", "nu-recent-meta",
        shortDate(entry.checkin) + " – " + shortDate(entry.checkout) +
        " · " + nights + (nights === 1 ? " night" : " nights") +
        " · " + entry.adults + (entry.adults == 1 ? " guest" : " guests")));
      btn.addEventListener("click", function () { replay(entry); });
      list.appendChild(btn);
    });
    section.hidden = false;
    updateRecentOverflowHint();

    var clear = document.getElementById("recentStaysClear");
    if (clear) {
      clear.onclick = function () {
        try { localStorage.removeItem(RECENT_KEY); } catch (e) {}
        section.hidden = true;
      };
    }
  }

  window.addEventListener("resize", updateRecentOverflowHint);
  var recentStaysScrollEl = document.getElementById("recentStaysList");
  if (recentStaysScrollEl) {
    recentStaysScrollEl.addEventListener("scroll", updateRecentOverflowHint, { passive: true });
  }

  function replay(entry) {
    var form = document.createElement("form");
    form.method = "POST";
    form.action = "/hotels/search";
    [["place_id", entry.place_id], ["place_name", entry.place_name],
     ["checkin", entry.checkin], ["checkout", entry.checkout],
     ["adults", entry.adults], ["rooms", entry.rooms]].forEach(function (pair) {
      var i = document.createElement("input");
      i.type = "hidden"; i.name = pair[0]; i.value = pair[1];
      form.appendChild(i);
    });
    document.body.appendChild(form);
    form.submit();
  }

  function recordSubmissions() {
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (!form || form.action.indexOf("/hotels/search") === -1) return;
      var get = function (n) {
        var f = form.querySelector('[name="' + n + '"]');
        return f ? f.value : "";
      };
      if (!get("place_id")) return;
      pushRecent({
        place_id: get("place_id"), place_name: get("place_name"),
        checkin: get("checkin"), checkout: get("checkout"),
        adults: get("adults") || "2", rooms: get("rooms") || "1"
      });
    }, true);
  }

  function rails() {
    document.querySelectorAll(".nu-arrow[data-rail]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var rail = document.getElementById("rail" + btn.dataset.rail.charAt(0).toUpperCase() + btn.dataset.rail.slice(1));
        if (!rail) return;
        rail.scrollBy({ left: Number(btn.dataset.dir) * rail.clientWidth * 0.8, behavior: "smooth" });
      });
    });
  }

  // Recording runs on every page that can submit a stay search, so the
  // landing rail fills up from real searches made anywhere on the site.
  recordSubmissions();

  return {
    init: init,
    modeToggle: modeToggle,
    rails: rails,
    showcase: showcase,
    renderRecent: renderRecent
  };
})();
