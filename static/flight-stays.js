(function () {
  "use strict";

  var shelf = document.getElementById("flightStays");
  if (!shelf) return;

  var recommendedRail = document.getElementById("flightStaysRecommended");
  var nearbyRail = document.getElementById("flightStaysNearby");
  var recommendedGroup = document.getElementById("flightStaysRecommendedGroup");
  var nearbyGroup = document.getElementById("flightStaysNearbyGroup");
  var summary = document.getElementById("flightStaysSummary");
  var allLink = document.getElementById("flightStaysAll");
  var recommendedNote = document.getElementById("flightStaysRecommendedNote");
  var nearbyNote = document.getElementById("flightStaysNearbyNote");
  var locationButton = document.getElementById("flightStaysLocation");

  function money(amount, currency) {
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency", currency: currency || "USD", maximumFractionDigits: 0,
      }).format(Number(amount) || 0);
    } catch (err) {
      return "US$" + Math.round(Number(amount) || 0).toLocaleString();
    }
  }

  function node(tag, className, content) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (content != null) element.textContent = content;
    return element;
  }

  function hotelCard(hotel, nights, priceDisplay) {
    var card = node("a", "flight-stay-card");
    card.href = hotel.url || "/hotels";

    var image = node("span", "flight-stay-card__image");
    if (hotel.photo) {
      var img = node("img");
      img.src = hotel.photo;
      img.alt = "";
      img.loading = "lazy";
      img.decoding = "async";
      img.addEventListener("error", function () { image.classList.add("is-empty"); img.remove(); });
      image.appendChild(img);
    } else {
      image.classList.add("is-empty");
    }
    image.appendChild(node("span", "home-deal-badge", "Member Price"));
    card.appendChild(image);

    var body = node("span", "flight-stay-card__body");
    if (hotel.stars) body.appendChild(node("span", "flight-stay-card__stars", "★".repeat(Math.min(5, hotel.stars))));
    body.appendChild(node("strong", "flight-stay-card__name", hotel.name || "Hotel"));
    body.appendChild(node("span", "flight-stay-card__place", [hotel.city, hotel.address].filter(Boolean).join(", ")));

    var foot = node("span", "flight-stay-card__foot");
    if (hotel.rating) {
      var rating = node("span", "flight-stay-card__rating" + (Number(hotel.rating) < 8 ? " flight-stay-card__rating--mid" : ""));
      rating.appendChild(node("b", null, hotel.rating));
      rating.appendChild(node("span", null, hotel.review_label || "Guest rating"));
      foot.appendChild(rating);
    }
    var offer = hotel.offer || {};
    var price = node("span", "flight-stay-card__price");
    var isNightly = priceDisplay === "nightly";
    var amount = Number(isNightly ? offer.nightly_amount : offer.total_amount) || 0;
    /* Expedia-style price block: struck public rate above the bold Member
       Price — the live rate IS the member price, the compare-at is that
       rate before the flat 10% Rewards discount. */
    if (amount > 0) {
      var strike = node("s", "flight-stay-card__strike", money(amount / 0.9, offer.currency));
      price.appendChild(strike);
    }
    price.appendChild(node("b", null, money(amount, offer.currency)));
    price.appendChild(node("span", null, isNightly ? "per night" : "total · " + nights + " night" + (nights === 1 ? "" : "s")));
    foot.appendChild(price);
    body.appendChild(foot);
    card.appendChild(body);
    return card;
  }

  function fillRail(rail, group, hotels, nights, priceDisplay) {
    rail.replaceChildren();
    if (!hotels || !hotels.length) {
      group.hidden = true;
      return;
    }
    hotels.forEach(function (hotel) { rail.appendChild(hotelCard(hotel, nights, priceDisplay)); });
  }

  var form = document.getElementById("unifiedForm");
  var destinationInput = form && form.querySelector('input[name="destination"]');
  var checkinInput = document.getElementById("departPicker");
  var checkoutInput = document.getElementById("returnPicker");
  var adultsInput = document.getElementById("topPassengers");
  var tripTypeInput = document.getElementById("tripType");
  var title = document.getElementById("flightStaysTitle");
  var requestId = 0;
  var activeRequestController = null;
  var activeRequestKey = "";
  var queuedUpdate;
  var destinationCommitTimer;
  var datesCommitTimer;
  var lastKey = "";
  var activeMode = "";
  var locationCoordinates = null;
  var locationRequested = false;
  var destinationCommitted = "";
  var datesCommittedKey = "";
  var stayCache = Object.create(null);
  var stayCacheOrder = [];
  var STAY_CACHE_TTL = 90000;
  var STAY_CACHE_LIMIT = 10;
  var DESTINATION_CODE_SETTLE_MS = 350;
  var DATE_SETTLE_MS = 500;

  if (!form || !destinationInput || !checkinInput || !checkoutInput) return;

  function isFlightsActive() {
    // The hotel shelf now shows on every home tab (Flights, Hotels, Ask AI)
    // — the below-fold content is identical regardless of which search panel
    // is open. The destination-driven variant still keys off the flight
    // form's inputs; with no destination it falls back to nearby hotels.
    return true;
  }

  function hasValidDates(checkin, checkout) {
    return /^\d{4}-\d{2}-\d{2}$/.test(checkin) && /^\d{4}-\d{2}-\d{2}$/.test(checkout) && checkout > checkin;
  }

  function normaliseDestination(value) {
    return (value || "").trim().replace(/\s+/g, " ").toUpperCase();
  }

  function datesKey(checkin, checkout) {
    return [checkin || "", checkout || ""].join("|");
  }

  function isAirportCode(value) {
    return /^[A-Z]{3}$/.test(normaliseDestination(value));
  }

  function isAbort(error) {
    return Boolean(error && error.name === "AbortError");
  }

  function abortActiveRequest() {
    requestId += 1;
    if (activeRequestController) {
      try { activeRequestController.abort(); } catch (e) {}
      activeRequestController = null;
    }
    activeRequestKey = "";
  }

  function beginRequest(mode, key) {
    // Identical request already rendered or is still loading. Reuse it rather
    // than sending another live-rate request whenever another form field
    // bubbles an event.
    if (activeMode === mode && activeRequestKey === key) return null;

    abortActiveRequest();
    activeMode = mode;
    activeRequestKey = key;
    var request = {
      id: ++requestId,
      mode: mode,
      key: key,
      controller: typeof window.AbortController === "function" ? new window.AbortController() : null,
    };
    activeRequestController = request.controller;
    return request;
  }

  function isCurrentRequest(request) {
    return Boolean(request) && request.id === requestId && activeMode === request.mode && activeRequestKey === request.key;
  }

  function finishRequest(request, failed) {
    if (!isCurrentRequest(request)) return false;
    activeRequestController = null;
    if (failed) activeRequestKey = "";
    return true;
  }

  function cachePayload(key, payload) {
    stayCache[key] = { payload: payload, expiresAt: Date.now() + STAY_CACHE_TTL };
    stayCacheOrder = stayCacheOrder.filter(function (cachedKey) { return cachedKey !== key; });
    stayCacheOrder.push(key);
    while (stayCacheOrder.length > STAY_CACHE_LIMIT) delete stayCache[stayCacheOrder.shift()];
  }

  function cachedPayload(key) {
    var cached = stayCache[key];
    if (!cached) return null;
    if (cached.expiresAt < Date.now()) {
      delete stayCache[key];
      stayCacheOrder = stayCacheOrder.filter(function (cachedKey) { return cachedKey !== key; });
      return null;
    }
    return cached.payload;
  }

  function showCachedPayload(mode, key, payload, options) {
    if (activeMode === mode && activeRequestKey === key) return true;
    abortActiveRequest();
    activeMode = mode;
    activeRequestKey = key;
    shelf.hidden = false;
    if (locationButton) locationButton.hidden = true;
    recommendedGroup.hidden = false;
    nearbyGroup.hidden = false;
    showPayload(payload, options);
    return true;
  }

  destinationCommitted = normaliseDestination(destinationInput.value);
  datesCommittedKey = datesKey(checkinInput.value, checkoutInput.value);

  function showSkeletons(rail) {
    rail.replaceChildren();
    for (var index = 0; index < 4; index += 1) rail.appendChild(node("div", "flight-stay-skeleton"));
  }

  function setGroupNotes(recommendedText, nearbyText) {
    if (recommendedNote) recommendedNote.textContent = recommendedText;
    if (nearbyNote) nearbyNote.textContent = nearbyText;
  }

  function showLoading(mode) {
    activeMode = mode;
    shelf.hidden = false;
    if (locationButton) locationButton.hidden = true;
    recommendedGroup.hidden = false;
    nearbyGroup.hidden = false;
    showSkeletons(recommendedRail);
    showSkeletons(nearbyRail);
  }

  function showPayload(payload, options) {
    var nights = Number(payload.nights) || 1;
    var priceDisplay = payload.price_display || "total";
    fillRail(recommendedRail, recommendedGroup, payload.recommended, nights, priceDisplay);
    fillRail(nearbyRail, nearbyGroup, payload.nearby, nights, priceDisplay);

    if (options.local) {
      if (title) title.textContent = "Hotels near you";
      if (summary) summary.textContent = "Current nightly rates near your location.";
      if (allLink) allLink.href = "/hotels";
      setGroupNotes("Recommended near you", "More stays near you");
    } else {
      if (payload.destination && title) title.textContent = "Hotels in " + payload.destination;
      if (payload.destination && summary) {
        summary.textContent = "Live prices for " + options.checkin + "–" + options.checkout + " in " + payload.destination + ".";
      }
      if (payload.browse_url && allLink) allLink.href = payload.browse_url;
      setGroupNotes("Selected for your trip", "More stays in the area");
    }
    if (locationButton) locationButton.hidden = true;

    if ((!payload.recommended || !payload.recommended.length) && (!payload.nearby || !payload.nearby.length)) {
      shelf.hidden = true;
    }
  }

  function loadLocationStays() {
    if (!locationCoordinates || !isFlightsActive()) return;
    var key = "local:" + Number(locationCoordinates.lat).toFixed(3) + ":" + Number(locationCoordinates.lng).toFixed(3);
    if (activeMode === "local" && activeRequestKey === key) return;
    var cached = cachedPayload(key);
    if (cached) {
      showCachedPayload("local", key, cached, { local: true });
      return;
    }
    var request = beginRequest("local", key);
    if (!request) return;
    showLoading("local");
    if (title) title.textContent = "Hotels near you";
    if (summary) summary.textContent = "Finding current nightly rates near your location…";
    if (allLink) allLink.href = "/hotels";
    setGroupNotes("Recommended near you", "More stays near you");

    var params = new URLSearchParams({
      lat: String(locationCoordinates.lat), lng: String(locationCoordinates.lng),
    });
    fetch("/api/hotels/flight-stays/nearby?" + params.toString(), request.controller ? { signal: request.controller.signal } : undefined)
      .then(function (response) {
        if (!response.ok) throw new Error("Unable to load nearby stays");
        return response.json();
      })
      .then(function (payload) {
        if (!finishRequest(request, false)) return;
        cachePayload(key, payload);
        showPayload(payload, { local: true });
      })
      .catch(function (error) {
        if (!isCurrentRequest(request) || isAbort(error)) return;
        finishRequest(request, true);
        shelf.hidden = true;
      });
  }

  function showLocationStays() {
    if (!isFlightsActive()) return;
    if (!locationCoordinates && window.SkairGeo) {
      // Another widget (or an earlier visit) may already have a fix cached
      // — reuse it silently instead of falling back to the button-prompt
      // state, so returning visitors never see "Use my location" again.
      locationCoordinates = window.SkairGeo.getCachedLocation();
    }
    if (locationCoordinates) {
      loadLocationStays();
      return;
    }
    if (locationRequested) return;
    showLocationPermissionState();
  }

  // Geolocation permission must be requested by the browser from a direct
  // visitor action. Keep this separate from the automatic shelf refresh so
  // the "Use my location" button is what opens the browser permission prompt
  // (SkairGeo itself refuses to prompt from a non-gesture call).
  function requestLocationStays() {
    if (!isFlightsActive()) return;
    if (!locationCoordinates && window.SkairGeo) {
      locationCoordinates = window.SkairGeo.getCachedLocation();
    }
    if (locationCoordinates) {
      loadLocationStays();
      return;
    }
    if (locationRequested || !navigator.geolocation) {
      showLocationPermissionState();
      return;
    }

    locationRequested = true;
    showLoading("local-pending");
    if (locationButton) {
      locationButton.hidden = false;
      locationButton.disabled = true;
      locationButton.textContent = "Requesting location…";
    }
    if (title) title.textContent = "Hotels near you";
    if (summary) summary.textContent = "Finding current nightly rates near your location…";
    if (allLink) allLink.href = "/hotels";
    setGroupNotes("Recommended near you", "More stays near you");

    var request = window.SkairGeo
      ? window.SkairGeo.requestLocation({ timeout: 10000 })
      : new Promise(function (resolve) {
          navigator.geolocation.getCurrentPosition(
            function (position) { resolve({ lat: position.coords.latitude, lng: position.coords.longitude }); },
            function () { resolve(null); },
            { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 }
          );
        });
    request.then(function (coords) {
      locationRequested = false;
      if (coords) {
        locationCoordinates = coords;
        if (activeMode === "local-pending") loadLocationStays();
      } else if (activeMode === "local-pending") {
        // Pass the real code through as-is — defaulting an unknown/missing
        // code to "1" (denied) was mislabeling genuine timeouts and other
        // failures as "blocked," which isn't true and sends the visitor to
        // the wrong fix (browser settings instead of just trying again).
        var errorCode = window.SkairGeo ? window.SkairGeo.getLastErrorCode() : null;
        showLocationPermissionState(errorCode ? { code: errorCode } : null);
      }
    });
  }

  function showLocationPermissionState(error) {
    shelf.hidden = false;
    activeMode = "location-unavailable";
    recommendedGroup.hidden = true;
    nearbyGroup.hidden = true;
    if (title) title.textContent = "Hotels near you";
    if (summary) {
      if (!navigator.geolocation) {
        summary.textContent = "Location is not supported by this browser.";
      } else if (error && error.code === 1) {
        summary.textContent =
          "You've blocked location for this site. Click the icon at the left of your address bar, " +
          "turn Location back to Allow, then use the button below again — no reload needed.";
      } else if (error && error.code === 3) {
        summary.textContent = "We could not determine your location. Please try again.";
      } else {
        summary.textContent = "Use your location to see current nightly rates nearby.";
      }
    }
    if (allLink) allLink.href = "/hotels";
    if (locationButton) {
      locationButton.hidden = false;
      locationButton.disabled = !navigator.geolocation;
      locationButton.textContent = "Use my location";
    }
  }

  function loadDestinationStays(destination, checkin, checkout, adults, key) {
    var requestKey = "destination:" + key;
    if (activeMode === "destination" && activeRequestKey === requestKey) return;
    lastKey = key;
    var cached = cachedPayload(requestKey);
    if (cached) {
      showCachedPayload("destination", requestKey, cached, { local: false, checkin: checkin, checkout: checkout });
      return;
    }
    var request = beginRequest("destination", requestKey);
    if (!request) return;
    showLoading("destination");
    if (title) title.textContent = "Hotels at your destination";
    if (summary) summary.textContent = "Finding stays with prices for your selected dates…";
    if (allLink) allLink.href = "/hotels";
    setGroupNotes("Selected for your trip", "More stays in the area");

    var params = new URLSearchParams({
      destination: destination, checkin: checkin, checkout: checkout, adults: adults,
    });
    fetch("/api/hotels/flight-stays?" + params.toString(), request.controller ? { signal: request.controller.signal } : undefined)
      .then(function (response) {
        if (!response.ok) throw new Error("Unable to load destination stays");
        return response.json();
      })
      .then(function (payload) {
        if (!finishRequest(request, false)) return;
        cachePayload(requestKey, payload);
        showPayload(payload, { local: false, checkin: checkin, checkout: checkout });
      })
      .catch(function (error) {
        if (!isCurrentRequest(request) || isAbort(error)) return;
        finishRequest(request, true);
        lastKey = "";
        shelf.hidden = true;
      });
  }

  function refresh() {
    var destination = destinationInput.value.trim();
    var checkin = checkinInput.value;
    var checkout = checkoutInput.value;
    var adults = adultsInput ? adultsInput.value : "1";
    var tripType = tripTypeInput ? tripTypeInput.value : "roundtrip";
    var key = [destination, checkin, checkout, adults, tripType].join("|");
    var currentDatesKey = datesKey(checkin, checkout);

    if (!isFlightsActive()) {
      abortActiveRequest();
      lastKey = "";
      activeMode = "";
      shelf.hidden = true;
      return;
    }

    if (tripType === "roundtrip" && destination && hasValidDates(checkin, checkout)) {
      // A range calendar updates its hidden fields independently. Only price
      // after both dates and the destination have settled, rather than using
      // an intermediate range while the visitor is still choosing it.
      if (destinationCommitted !== normaliseDestination(destination) || datesCommittedKey !== currentDatesKey) return;
      loadDestinationStays(destination, checkin, checkout, adults, key);
      return;
    }

    // While a destination or date range is incomplete, retain the existing
    // shelf instead of replacing it with a new location lookup on every input
    // pause. Clearing the route or switching away from round-trip restores the
    // nearby defaults as before.
    if (tripType === "roundtrip" && destination) return;

    lastKey = "";
    if (activeMode === "destination" && activeRequestController) abortActiveRequest();
    showLocationStays();
  }

  function queueRefresh(delay) {
    window.clearTimeout(queuedUpdate);
    queuedUpdate = window.setTimeout(refresh, typeof delay === "number" ? delay : 180);
  }

  function abortPendingDestinationRequest() {
    if (activeMode === "destination" && activeRequestController) abortActiveRequest();
  }

  function scheduleDestinationCommit() {
    destinationCommitted = "";
    window.clearTimeout(destinationCommitTimer);
    abortPendingDestinationRequest();
    var candidate = destinationInput.value.trim();
    if (!candidate) {
      queueRefresh(120);
      return;
    }

    // Airport autocomplete commits a three-letter code through an input event
    // (not a change event). Treat that as a completed selection after a very
    // short settle window; free-typed city names still wait for blur/change.
    if (isAirportCode(candidate)) {
      var committedValue = normaliseDestination(candidate);
      destinationCommitTimer = window.setTimeout(function () {
        if (normaliseDestination(destinationInput.value) !== committedValue) return;
        destinationCommitted = committedValue;
        queueRefresh(0);
      }, DESTINATION_CODE_SETTLE_MS);
    }
  }

  function commitDestination() {
    window.clearTimeout(destinationCommitTimer);
    destinationCommitted = normaliseDestination(destinationInput.value);
    queueRefresh(0);
  }

  function scheduleDateCommit() {
    datesCommittedKey = "";
    window.clearTimeout(datesCommitTimer);
    abortPendingDestinationRequest();
    datesCommitTimer = window.setTimeout(function () {
      datesCommittedKey = datesKey(checkinInput.value, checkoutInput.value);
      queueRefresh(0);
    }, DATE_SETTLE_MS);
  }

  destinationInput.addEventListener("input", scheduleDestinationCommit);
  destinationInput.addEventListener("change", commitDestination);
  checkinInput.addEventListener("input", scheduleDateCommit);
  checkinInput.addEventListener("change", scheduleDateCommit);
  checkoutInput.addEventListener("input", scheduleDateCommit);
  checkoutInput.addEventListener("change", scheduleDateCommit);
  if (adultsInput) {
    adultsInput.addEventListener("input", function () { queueRefresh(120); });
    adultsInput.addEventListener("change", function () { queueRefresh(120); });
  }
  if (tripTypeInput) tripTypeInput.addEventListener("change", function () { queueRefresh(0); });

  var swapButton = document.getElementById("swapBtn");
  if (swapButton) {
    swapButton.addEventListener("click", function () {
      // The existing swap handler changes values directly, so it does not emit
      // input/change events for the destination field.
      window.setTimeout(commitDestination, 0);
    });
  }
  var paxPanel = document.getElementById("gfPaxPanel");
  if (paxPanel && adultsInput) {
    paxPanel.addEventListener("click", function () { window.setTimeout(function () { queueRefresh(120); }, 0); });
  }
  if (locationButton) {
    locationButton.addEventListener("click", function () {
      requestLocationStays();
    });
  }
  document.querySelectorAll(".home-product-tab").forEach(function (tab) {
    tab.addEventListener("click", function () { window.setTimeout(refresh, 0); });
  });
  window.addEventListener("pagehide", function () {
    window.clearTimeout(queuedUpdate);
    window.clearTimeout(destinationCommitTimer);
    window.clearTimeout(datesCommitTimer);
    abortActiveRequest();
  }, { once: true });
  // Let the inline edit-search/session restore scripts populate the form
  // first. On an already-saved trip this avoids opening a location-priced
  // request that will be replaced moments later by the destination request.
  queueRefresh(180);
})();
