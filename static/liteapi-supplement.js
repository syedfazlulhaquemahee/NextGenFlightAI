/* LiteAPI (second flight provider) results arrive slower than Duffel's —
 * confirmed live: LiteAPI's real search aggregation takes ~15-20s, and
 * there's no way to get partial results early (it's a plain buffered JSON
 * response, not a client-consumable stream). Rather than block the fast
 * Duffel-only page render on that, this script fires once after the page
 * has already loaded, fetches LiteAPI's results in the background, and
 * swaps in updated (cheaper) cards if anything comes back — non-blocking,
 * non-destructive (no document.write — this page is already live and
 * interactive by the time this runs).
 */
(() => {
  const form = document.getElementById("resultsQueryForm");
  const cardsEl = document.getElementById("resultsCards");
  if (!form || !cardsEl) return;

  const tripType = (form.querySelector('[name="trip_type"]')?.value || "").toLowerCase();
  if (tripType && tripType !== "oneway" && tripType !== "roundtrip") return; // multicity/flex — server-side scope cut, skip the request entirely

  const currentPrice = () => {
    const el = cardsEl.querySelector("[data-price]");
    const raw = el?.getAttribute("data-price");
    const n = raw ? parseFloat(raw) : NaN;
    return Number.isFinite(n) ? n : null;
  };

  const showToast = (text) => {
    const toast = document.createElement("div");
    toast.textContent = text;
    toast.style.cssText =
      "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:9999;" +
      "background:rgba(15,23,42,.92);color:#fff;padding:10px 18px;border-radius:999px;" +
      "font:600 13px/1.4 -apple-system,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.25);" +
      "opacity:0;transition:opacity .25s ease;";
    document.body.appendChild(toast);
    requestAnimationFrame(() => { toast.style.opacity = "1"; });
    setTimeout(() => {
      toast.style.opacity = "0";
      setTimeout(() => toast.remove(), 300);
    }, 3200);
  };

  const indicator = document.createElement("span");
  indicator.textContent = "Checking more fares…";
  indicator.style.cssText = "display:inline-flex;align-items:center;gap:6px;font:600 11px/1 -apple-system,sans-serif;color:var(--muted,#667085);opacity:.85;margin-left:8px;";
  const sortSelect = document.getElementById("resultsTopSortSelectManual") || document.getElementById("resultsSortInput");
  sortSelect?.parentElement?.appendChild(indicator);

  const priceBefore = currentPrice();

  fetch("/search/liteapi-supplement", { method: "POST", body: new FormData(form) })
    .then((r) => (r.ok ? r.json() : { updated: false }))
    .then((data) => {
      indicator.remove();
      if (!data || !data.updated || !data.html) return;

      const parsed = new DOMParser().parseFromString(data.html, "text/html");
      const newCards = parsed.getElementById("resultsCards");
      if (!newCards || !newCards.innerHTML.trim()) return;

      const newPrice = (() => {
        const el = newCards.querySelector("[data-price]");
        const raw = el?.getAttribute("data-price");
        const n = raw ? parseFloat(raw) : NaN;
        return Number.isFinite(n) ? n : null;
      })();

      // Only swap when it actually changes the cheapest price shown —
      // merging in more same-or-worse options isn't worth rearranging
      // 200 live cards under the user for.
      if (priceBefore == null || newPrice == null || newPrice >= priceBefore) return;

      cardsEl.innerHTML = newCards.innerHTML;
      showToast("Found a cheaper fare — prices updated");
    })
    .catch(() => {
      indicator.remove(); // best-effort enhancement — a failed fetch just leaves the fast Duffel results as-is
    });
})();
