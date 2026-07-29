/**
 * Skairova Voice AI — travel intent bridge.
 *
 * Deliberately does NOT parse travel intent itself. It calls the exact same
 * `/search/ai-parse-preview` endpoint the text AI search bar already warms
 * up while the user types (see the inline script in templates/index.html),
 * so voice and text search share one parser, one cache, and one set of
 * "what counts as a valid trip" rules. This module only adds a confidence
 * read on top, so the overlay can ask a clarifying question instead of a
 * flat "didn't understand" when the parse comes back thin.
 */

export class TravelParseFailedError extends Error {
  constructor(message) {
    super(message || "Could not understand that request.");
    this.name = "TravelParseFailedError";
  }
}

/**
 * @param {string} text
 * @param {AbortSignal} [signal]
 * @returns {Promise<{ok: boolean, parseToken: string, preview: Record<string, any>}>}
 */
export async function parseTravelIntent(text, signal) {
  const trimmed = (text || "").trim();
  if (!trimmed) throw new TravelParseFailedError("We didn't catch that.");

  let response;
  try {
    response = await fetch("/search/ai-parse-preview", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-Requested-With": "fetch",
      },
      body: JSON.stringify({ ai_text: trimmed }),
      signal,
    });
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new TravelParseFailedError("Could not reach the search server.");
  }

  if (!response.ok) throw new TravelParseFailedError();
  const payload = await response.json().catch(() => null);
  if (!payload || !payload.ok) throw new TravelParseFailedError();

  return {
    ok: true,
    parseToken: payload.parse_token || "",
    preview: payload.preview || {},
  };
}

/**
 * Heuristic confidence read: do we have enough of a trip to search on, or
 * should we ask a clarifying question instead of guessing? Mirrors the
 * fields `/search` actually requires (origin+destination and/or a flex month).
 *
 * @param {Record<string, any>} preview
 * @returns {{confident: boolean, question: string|null}}
 */
export function assessConfidence(preview) {
  const origin = preview && preview.origin;
  const destination = preview && preview.destination;
  const flexMonth = preview && preview.flex_month;

  // Same city both ends is never a searchable trip — almost always a
  // misheard/duplicated place name. Ask rather than search JFK → JFK.
  if (origin && destination && String(origin).toUpperCase() === String(destination).toUpperCase()) {
    return { confident: false, question: "Origin and destination sound the same — where to?" };
  }

  if (origin && destination) return { confident: true, question: null };
  if (flexMonth && destination) return { confident: true, question: null };

  if (!origin && destination) {
    return { confident: false, question: `Got ${destination} — which city are you flying from?` };
  }
  if (origin && !destination) {
    return { confident: false, question: `Leaving ${origin} — where do you want to go?` };
  }
  return { confident: false, question: "Say where from and where to, like “Dhaka to London”." };
}
