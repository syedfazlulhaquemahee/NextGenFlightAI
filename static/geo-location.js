/**
 * Skairova — shared geolocation helper.
 *
 * Three separate widgets (popular-flights on the home page, the hotel
 * showcase rails, and the flight-results "hotels near you" shelf) used to
 * each call navigator.geolocation.getCurrentPosition() on their own, with
 * no shared cache — and one of them fired automatically on page load with
 * no user gesture. That combination is exactly what makes a browser (and a
 * user) feel like they're being asked for their location on every visit.
 *
 * Every geolocation caller in this app should go through here instead of
 * calling navigator.geolocation directly:
 *   - A resolved position is cached (localStorage) for CACHE_TTL_MS, so once
 *     ANY widget gets a fix, every other widget this visit — and the next
 *     several visits — reuses it with zero new browser prompts.
 *   - Automatic (gesture-less) callers must pass {auto: true}. In that mode
 *     this checks navigator.permissions.query first and only calls
 *     getCurrentPosition if the browser reports access as already granted.
 *     Only a real user action (a button click, opts.auto falsy/omitted) is
 *     allowed to trigger a fresh permission prompt.
 *   - A recent denial is remembered so automatic callers stop trying for a
 *     while, instead of silently re-attempting (and re-showing their own
 *     "finding your location" UI) on every page load.
 */
(function (window) {
  "use strict";

  var CACHE_KEY = "skair_geo_v1";
  var CACHE_TTL_MS = 60 * 60 * 1000; // 1 hour: long enough to skip re-asking all visit long, short enough that a real move gets noticed same day.
  var DENIED_KEY = "skair_geo_denied_v1";
  var DENIED_TTL_MS = 24 * 60 * 60 * 1000;
  var lastErrorCode = null; // GeolocationPositionError.code from the most recent failed request, for callers that want a precise message.

  function readCache() {
    try {
      var raw = window.localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      var data = JSON.parse(raw);
      if (!data || typeof data.lat !== "number" || typeof data.lng !== "number") return null;
      if (Date.now() - (data.at || 0) > CACHE_TTL_MS) return null;
      return { lat: data.lat, lng: data.lng };
    } catch (e) {
      return null;
    }
  }

  function writeCache(coords) {
    try {
      window.localStorage.setItem(CACHE_KEY, JSON.stringify({ lat: coords.lat, lng: coords.lng, at: Date.now() }));
    } catch (e) { /* storage unavailable (private mode, quota) — just skip caching */ }
  }

  function wasRecentlyDenied() {
    try {
      var at = Number(window.localStorage.getItem(DENIED_KEY)) || 0;
      return at > 0 && Date.now() - at < DENIED_TTL_MS;
    } catch (e) {
      return false;
    }
  }

  function rememberDenied() {
    try { window.localStorage.setItem(DENIED_KEY, String(Date.now())); } catch (e) {}
  }

  function clearDenied() {
    try { window.localStorage.removeItem(DENIED_KEY); } catch (e) {}
  }

  function queryPermissionState() {
    if (!window.navigator.permissions || !window.navigator.permissions.query) {
      return Promise.resolve(null); // Unsupported — caller treats as unknown.
    }
    return window.navigator.permissions
      .query({ name: "geolocation" })
      .then(function (status) { return status.state; }) // "granted" | "denied" | "prompt"
      .catch(function () { return null; });
  }

  function askBrowser(opts) {
    return new Promise(function (resolve) {
      window.navigator.geolocation.getCurrentPosition(
        function (pos) {
          var coords = { lat: pos.coords.latitude, lng: pos.coords.longitude };
          lastErrorCode = null;
          writeCache(coords);
          clearDenied();
          resolve(coords);
        },
        function (err) {
          lastErrorCode = err ? err.code : null;
          if (err && err.code === 1) rememberDenied(); // PERMISSION_DENIED
          resolve(null);
        },
        { enableHighAccuracy: false, timeout: opts.timeout || 8000, maximumAge: CACHE_TTL_MS }
      );
    });
  }

  /**
   * @param {Object} [opts]
   * @param {boolean} [opts.auto] - true for automatic/gesture-less callers.
   *   Auto mode never triggers a fresh permission prompt: it only resolves
   *   a position that's already cached, or already granted by the browser.
   * @param {number} [opts.timeout]
   * @returns {Promise<{lat:number, lng:number}|null>}
   */
  function requestLocation(opts) {
    opts = opts || {};
    var cached = readCache();
    if (cached) return Promise.resolve(cached);
    if (!window.navigator.geolocation) return Promise.resolve(null);

    if (!opts.auto) {
      // A real user gesture — always fine to ask.
      return askBrowser(opts);
    }

    if (wasRecentlyDenied()) return Promise.resolve(null);

    return queryPermissionState().then(function (state) {
      if (state === "granted") return askBrowser(opts);
      return null; // "denied" or "prompt" (undecided) — don't surface a prompt nobody asked for.
    });
  }

  window.SkairGeo = {
    requestLocation: requestLocation,
    getCachedLocation: readCache,
    // The GeolocationPositionError.code from the most recent failed
    // request (1 = denied, 2 = unavailable, 3 = timeout), or null if the
    // last attempt succeeded / nothing has failed yet. Lets a caller that
    // wants a precise "blocked" vs "couldn't determine location" message
    // get one without requestLocation()'s resolved value needing to carry
    // more than just the coordinates.
    getLastErrorCode: function () { return lastErrorCode; },
  };
})(window);
