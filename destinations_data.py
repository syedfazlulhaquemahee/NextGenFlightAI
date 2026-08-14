"""
Curated content for the homepage "Popular destinations" experience:
richer cards, travel-intent categories, destination landing pages, and the
comparison tool.

Everything here is editorial content (hooks, best-time-to-visit, budget
ranges, trait ratings) written honestly as general travel guidance — not
live data and not dressed up as AI-generated or data-driven. Live pricing is
fetched separately at request time from the app's real flight-search
backend (see /api/destination-prices in app.py); this module never invents
prices, review counts, or traveler statistics.

Trait ratings (1-5) are a subjective editorial judgment call, presented in
the UI as "Our take" — a travel-guide opinion, not a claim of measured data.
"""

from __future__ import annotations

CATEGORIES = [
    {"slug": "study-abroad", "label": "Study abroad", "icon": "🎓"},
    {"slug": "honeymoon", "label": "Honeymoon", "icon": "💑"},
    {"slug": "beach", "label": "Beach escape", "icon": "🏖"},
    {"slug": "budget", "label": "Budget friendly", "icon": "💰"},
]

DESTINATIONS = [
    {
        "slug": "paris",
        "city": "Paris",
        "country": "France",
        "flag": "🇫🇷",
        "code": "PAR",
        "photo": "paris.jpg",
        "alt": "The Eiffel Tower and the Seine river in Paris, France",
        "hook": "Cafes, the Seine, and a skyline built around one iron tower",
        "best_time": "Apr – Jun & Sep – Oct",
        "categories": [],
        "traits": {"food": 5, "nightlife": 4, "culture": 5, "budget": 2, "nature": 2, "shopping": 5},
        "budget_guide": {"budget": "$40–70/night hostels & budget hotels", "mid": "$150–260/night mid-range hotels", "luxury": "$400+/night"},
        "visa_note": "Schengen-area entry rules apply for most non-EU visitors.",
    },
    {
        "slug": "tokyo",
        "city": "Tokyo",
        "country": "Japan",
        "flag": "🇯🇵",
        "code": "TYO",
        "photo": "tokyo.jpg",
        "alt": "Skyscrapers of Shinjuku in Tokyo, Japan",
        "hook": "Ancient temples meet neon-lit, futuristic energy",
        "best_time": "Mar – May & Oct – Nov",
        "categories": ["study-abroad"],
        "traits": {"food": 5, "nightlife": 5, "culture": 5, "budget": 3, "nature": 2, "shopping": 5},
        "budget_guide": {"budget": "$30–55/night hostels & capsule hotels", "mid": "$100–180/night hotels", "luxury": "$300+/night"},
        "visa_note": "Many nationalities get visa-free short stays; check Japan's official requirements for yours.",
    },
    {
        "slug": "rome",
        "city": "Rome",
        "country": "Italy",
        "flag": "🇮🇹",
        "code": "ROM",
        "photo": "rome.jpg",
        "alt": "The Trevi Fountain in Rome, Italy",
        "hook": "Three thousand years of history around every corner",
        "best_time": "Apr – May & Sep – Oct",
        "categories": [],
        "traits": {"food": 5, "nightlife": 3, "culture": 5, "budget": 3, "nature": 2, "shopping": 3},
        "budget_guide": {"budget": "$35–60/night hostels", "mid": "$120–220/night hotels", "luxury": "$350+/night"},
        "visa_note": "Schengen-area entry rules apply for most non-EU visitors.",
    },
    {
        "slug": "london",
        "city": "London",
        "country": "United Kingdom",
        "flag": "🇬🇧",
        "code": "LON",
        "photo": "london.jpg",
        "alt": "The London skyline in the United Kingdom",
        "hook": "A world capital of theatre, museums, and university life",
        "best_time": "May – Sep",
        "categories": ["study-abroad"],
        "traits": {"food": 4, "nightlife": 5, "culture": 5, "budget": 2, "nature": 2, "shopping": 5},
        "budget_guide": {"budget": "$45–75/night hostels", "mid": "$160–280/night hotels", "luxury": "$400+/night"},
        "visa_note": "A student visa is required for most degree programs; check UKVI for current rules.",
    },
    {
        "slug": "dubai",
        "city": "Dubai",
        "country": "United Arab Emirates",
        "flag": "🇦🇪",
        "code": "DXB",
        "photo": "dubai.jpg",
        "alt": "The Burj Khalifa in Dubai, United Arab Emirates",
        "hook": "Record-breaking towers, desert dunes, and duty-free shopping",
        "best_time": "Nov – Mar",
        "categories": [],
        "traits": {"food": 4, "nightlife": 4, "culture": 3, "budget": 2, "nature": 3, "shopping": 5},
        "budget_guide": {"budget": "$45–70/night budget hotels", "mid": "$140–250/night hotels", "luxury": "$400+/night"},
        "visa_note": "Many nationalities get visa-on-arrival; check UAE's official requirements for yours.",
    },
    {
        "slug": "bali",
        "city": "Bali",
        "country": "Indonesia",
        "flag": "🇮🇩",
        "code": "DPS",
        "photo": "bali.jpg",
        "alt": "Tanah Lot sea temple in Bali, Indonesia",
        "hook": "Rice terraces, sea temples, and sunset surf beaches",
        "best_time": "Apr – Oct (dry season)",
        "categories": ["honeymoon", "beach", "budget"],
        "traits": {"food": 4, "nightlife": 3, "culture": 4, "budget": 5, "nature": 5, "shopping": 3},
        "budget_guide": {"budget": "$10–25/night guesthouses", "mid": "$40–90/night villas & hotels", "luxury": "$150+/night"},
        "visa_note": "Most visitors can get a visa-on-arrival for short stays; confirm current Indonesian entry rules.",
    },
    {
        "slug": "barcelona",
        "city": "Barcelona",
        "country": "Spain",
        "flag": "🇪🇸",
        "code": "BCN",
        "photo": "barcelona.jpg",
        "alt": "Evening light over Barcelona, Spain",
        "hook": "Gaudí architecture, tapas, and Mediterranean beaches in one city",
        "best_time": "May – Jun & Sep",
        "categories": ["beach"],
        "traits": {"food": 5, "nightlife": 5, "culture": 4, "budget": 3, "nature": 3, "shopping": 4},
        "budget_guide": {"budget": "$30–55/night hostels", "mid": "$110–200/night hotels", "luxury": "$320+/night"},
        "visa_note": "Schengen-area entry rules apply for most non-EU visitors.",
    },
    {
        "slug": "sydney",
        "city": "Sydney",
        "country": "Australia",
        "flag": "🇦🇺",
        "code": "SYD",
        "photo": "sydney.jpg",
        "alt": "Sydney Opera House and Harbour Bridge at dusk in Australia",
        "hook": "Harbour views, beach culture, and an iconic Opera House skyline",
        "best_time": "Sep – Nov & Mar – May",
        "categories": ["beach"],
        "traits": {"food": 4, "nightlife": 4, "culture": 3, "budget": 2, "nature": 5, "shopping": 3},
        "budget_guide": {"budget": "$40–65/night hostels", "mid": "$150–260/night hotels", "luxury": "$380+/night"},
        "visa_note": "An eVisitor or ETA is required for most short-stay visitors to Australia.",
    },
    {
        "slug": "maldives",
        "city": "Maldives",
        "country": "Maldives",
        "flag": "🇲🇻",
        "code": "MLE",
        "photo": "maldives.jpg",
        "alt": "Malé, the capital city and gateway to the Maldives atolls",
        "hook": "Overwater villas and turquoise lagoons made for slowing down",
        "best_time": "Nov – Apr (dry season)",
        "categories": ["honeymoon", "beach"],
        "traits": {"food": 3, "nightlife": 1, "culture": 2, "budget": 1, "nature": 5, "shopping": 1},
        "budget_guide": {"budget": "$60–100/night guesthouses (local islands)", "mid": "$250–450/night resorts", "luxury": "$700+/night overwater villas"},
        "visa_note": "Most nationalities receive a free 30- to 90-day visa on arrival.",
    },
    {
        "slug": "santorini",
        "city": "Santorini",
        "country": "Greece",
        "flag": "🇬🇷",
        "code": "JTR",
        "photo": "santorini.jpg",
        "alt": "Whitewashed clifftop village of Oia in Santorini, Greece at sunset",
        "hook": "Whitewashed clifftop villages built around volcanic sunsets",
        "best_time": "May – Jun & Sep – Oct",
        "categories": ["honeymoon"],
        "traits": {"food": 4, "nightlife": 3, "culture": 3, "budget": 2, "nature": 4, "shopping": 3},
        "budget_guide": {"budget": "$50–90/night guesthouses", "mid": "$180–320/night caldera-view hotels", "luxury": "$450+/night"},
        "visa_note": "Schengen-area entry rules apply for most non-EU visitors.",
    },
    {
        "slug": "toronto",
        "city": "Toronto",
        "country": "Canada",
        "flag": "🇨🇦",
        "code": "YTO",
        "photo": "toronto.jpg",
        "alt": "The Toronto skyline viewed from the harbour, Canada",
        "hook": "A diverse, walkable city anchored by top-ranked universities",
        "best_time": "Jun – Sep",
        "categories": ["study-abroad"],
        "traits": {"food": 4, "nightlife": 3, "culture": 4, "budget": 3, "nature": 2, "shopping": 3},
        "budget_guide": {"budget": "$35–60/night hostels", "mid": "$130–220/night hotels", "luxury": "$320+/night"},
        "visa_note": "A study permit is required for most programs longer than 6 months; check IRCC for current rules.",
    },
    {
        "slug": "melbourne",
        "city": "Melbourne",
        "country": "Australia",
        "flag": "🇦🇺",
        "code": "MEL",
        "photo": "melbourne.jpg",
        "alt": "The Melbourne skyline along the Yarra River, Australia",
        "hook": "Laneway cafes, live music, and a huge international student scene",
        "best_time": "Mar – May & Sep – Nov",
        "categories": ["study-abroad"],
        "traits": {"food": 4, "nightlife": 4, "culture": 4, "budget": 2, "nature": 2, "shopping": 3},
        "budget_guide": {"budget": "$35–60/night hostels", "mid": "$140–240/night hotels", "luxury": "$350+/night"},
        "visa_note": "A student visa is required for most degree programs; check Australia's Department of Home Affairs for current rules.",
    },
    {
        "slug": "bangkok",
        "city": "Bangkok",
        "country": "Thailand",
        "flag": "🇹🇭",
        "code": "BKK",
        "photo": "bangkok.jpg",
        "alt": "The Bangkok skyline in Thailand",
        "hook": "Street food, temples, and one of Asia's best travel bargains",
        "best_time": "Nov – Feb (cool, dry season)",
        "categories": ["beach", "budget"],
        "traits": {"food": 5, "nightlife": 5, "culture": 4, "budget": 5, "nature": 2, "shopping": 4},
        "budget_guide": {"budget": "$10–20/night hostels", "mid": "$35–70/night hotels", "luxury": "$150+/night"},
        "visa_note": "Many nationalities get visa-free short stays; check Thailand's official requirements for yours.",
    },
    {
        "slug": "istanbul",
        "city": "Istanbul",
        "country": "Turkey",
        "flag": "🇹🇷",
        "code": "IST",
        "photo": "istanbul.jpg",
        "alt": "The historical peninsula and modern skyline of Istanbul, Turkey",
        "hook": "Where Europe meets Asia across the Bosphorus strait",
        "best_time": "Apr – May & Sep – Oct",
        "categories": ["budget"],
        "traits": {"food": 5, "nightlife": 3, "culture": 5, "budget": 4, "nature": 2, "shopping": 5},
        "budget_guide": {"budget": "$15–30/night hostels", "mid": "$50–100/night hotels", "luxury": "$180+/night"},
        "visa_note": "Many nationalities can get an e-Visa online before arrival; check Turkey's official portal.",
    },
]

# Domestic (United States) routes for the homepage's "Popular flights near
# you" widget — a lighter schema than DESTINATIONS since that section has no
# package-builder, comparison tool, or destination landing page to feed.
DOMESTIC_DESTINATIONS = [
    {
        "slug": "los-angeles",
        "city": "Los Angeles",
        "country": "United States",
        "code": "LAX",
        "photo": "losangeles.jpg",
        "alt": "The downtown Los Angeles skyline at dusk with palm trees in the foreground",
    },
    {
        "slug": "miami",
        "city": "Miami",
        "country": "United States",
        "code": "MIA",
        "photo": "miami.jpg",
        "alt": "Miami's waterfront skyscrapers along Biscayne Bay",
    },
    {
        "slug": "las-vegas",
        "city": "Las Vegas",
        "country": "United States",
        "code": "LAS",
        "photo": "lasvegas.jpg",
        "alt": "The Las Vegas Strip lit up at night",
    },
    {
        "slug": "chicago",
        "city": "Chicago",
        "country": "United States",
        "code": "ORD",
        "photo": "chicago.jpg",
        "alt": "Millennium Park and the Cloud Gate sculpture in Chicago",
    },
    {
        "slug": "san-francisco",
        "city": "San Francisco",
        "country": "United States",
        "code": "SFO",
        "photo": "sanfrancisco.jpg",
        "alt": "The Golden Gate Bridge in San Francisco at night",
    },
    {
        "slug": "orlando",
        "city": "Orlando",
        "country": "United States",
        "code": "MCO",
        "photo": "orlando.jpg",
        "alt": "The downtown Orlando skyline reflected in Lake Eola at dusk",
    },
]

_BY_SLUG = {d["slug"]: d for d in DESTINATIONS}
_BY_CODE = {d["code"]: d for d in DESTINATIONS}
_DOMESTIC_BY_CODE = {d["code"]: d for d in DOMESTIC_DESTINATIONS}


def get_destination(slug: str) -> dict | None:
    return _BY_SLUG.get((slug or "").strip().lower())


def get_destination_by_code(code: str) -> dict | None:
    return _BY_CODE.get((code or "").strip().upper())


def get_domestic_destination_by_code(code: str) -> dict | None:
    return _DOMESTIC_BY_CODE.get((code or "").strip().upper())


def destinations_for_category(category_slug: str) -> list[dict]:
    return [d for d in DESTINATIONS if category_slug in d["categories"]]
