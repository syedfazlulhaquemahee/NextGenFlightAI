from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime

app = Flask(__name__)

# -----------------------------
# Mock "AI parsing" (Gemini later)
# -----------------------------
def parse_ai_query(text: str) -> dict:
    """
    Very simple placeholder parser.
    Later: replace with Gemini response -> structured JSON fields.
    """
    t = (text or "").lower()

    # super basic guesses
    nonstop = "nonstop" in t or "no stops" in t or "no layover" in t
    sort = "cheapest" if "cheap" in t or "under $" in t else "fastest" if "fast" in t or "earliest" in t else "recommended"

    # placeholder values
    return {
        "origin": "JFK" if "jfk" in t else "",
        "destination": "DEL" if "delhi" in t or "del" in t else "",
        "depart_date": "",
        "return_date": "",
        "passengers": 1,
        "cabin": "ECONOMY",
        "nonstop": nonstop,
        "sort": sort,
        "raw_text": text.strip()
    }

# -----------------------------
# Mock "Amadeus flight search"
# -----------------------------
def search_flights(params: dict) -> list[dict]:
    """
    Later: call Amadeus API with structured params.
    For now: return mock flight cards that look real.
    """
    flights = [
        {
            "airline": "Delta",
            "price": 642,
            "duration_min": 520,
            "stops": 0,
            "depart": "07:15",
            "arrive": "15:55",
            "route": f"{params.get('origin','JFK') or 'JFK'} → {params.get('destination','LAX') or 'LAX'}",
            "booking_url": "https://www.delta.com"
        },
        {
            "airline": "United",
            "price": 599,
            "duration_min": 610,
            "stops": 1,
            "depart": "09:40",
            "arrive": "19:05",
            "route": f"{params.get('origin','JFK') or 'JFK'} → {params.get('destination','LAX') or 'LAX'}",
            "booking_url": "https://www.united.com"
        },
        {
            "airline": "American",
            "price": 705,
            "duration_min": 495,
            "stops": 0,
            "depart": "12:10",
            "arrive": "20:25",
            "route": f"{params.get('origin','JFK') or 'JFK'} → {params.get('destination','LAX') or 'LAX'}",
            "booking_url": "https://www.aa.com"
        },
        {
            "airline": "JetBlue",
            "price": 560,
            "duration_min": 680,
            "stops": 1,
            "depart": "06:10",
            "arrive": "16:30",
            "route": f"{params.get('origin','JFK') or 'JFK'} → {params.get('destination','LAX') or 'LAX'}",
            "booking_url": "https://www.jetblue.com"
        },
        {
            "airline": "Alaska",
            "price": 630,
            "duration_min": 540,
            "stops": 0,
            "depart": "15:05",
            "arrive": "23:15",
            "route": f"{params.get('origin','JFK') or 'JFK'} → {params.get('destination','LAX') or 'LAX'}",
            "booking_url": "https://www.alaskaair.com"
        },
    ]

    # Apply nonstop filter
    if params.get("nonstop"):
        flights = [f for f in flights if f["stops"] == 0]

    # Sort
    sort = params.get("sort", "recommended")
    if sort == "cheapest":
        flights.sort(key=lambda x: x["price"])
    elif sort == "fastest":
        flights.sort(key=lambda x: x["duration_min"])

    return flights

def minutes_to_hm(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    return f"{h}h {m}m"

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/search", methods=["POST"])
def search():
    mode = request.form.get("mode")  # "standard" or "ai"

    if mode == "ai":
        ai_text = request.form.get("ai_text", "")
        params = parse_ai_query(ai_text)
    else:
        # standard form
        params = {
            "origin": request.form.get("origin", "").strip().upper(),
            "destination": request.form.get("destination", "").strip().upper(),
            "depart_date": request.form.get("depart_date", "").strip(),
            "return_date": request.form.get("return_date", "").strip(),
            "passengers": int(request.form.get("passengers", "1")),
            "cabin": request.form.get("cabin", "ECONOMY"),
            "nonstop": request.form.get("nonstop") == "on",
            "sort": request.form.get("sort", "recommended"),
            "raw_text": ""
        }

    # Minimal validation (UI-focused)
    if not params.get("origin") or not params.get("destination"):
        return render_template(
            "results.html",
            query=params,
            flights=[],
            error="Please enter both origin and destination (e.g., JFK → LAX).",
            minutes_to_hm=minutes_to_hm
        )

    flights = search_flights(params)

    if not flights:
        return render_template(
            "results.html",
            query=params,
            flights=[],
            error="No flights found with your filters. Try turning off ‘Nonstop only’ or switching sort.",
            minutes_to_hm=minutes_to_hm
        )

    return render_template(
        "results.html",
        query=params,
        flights=flights,
        error="",
        minutes_to_hm=minutes_to_hm
    )

if __name__ == "__main__":
    app.run(debug=True)
