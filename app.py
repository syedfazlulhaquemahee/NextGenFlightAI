from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
import os
import google.generativeai as genai
import json

genai.configure(api_key="None")


model = genai.GenerativeModel("models/gemini-2.5-flash")



app = Flask(__name__)

# -----------------------------
# Mock "AI parsing" (Gemini later)
# -----------------------------
def parse_ai_flight_request(user_text):
    prompt = f"""
You are a flight search assistant.

Convert the user's request into valid JSON with these fields:
- origin (IATA code or null)
- destination (IATA code or null)
- depart_date (YYYY-MM-DD or null)
- return_date (YYYY-MM-DD or null)
- passengers (integer, default 1)
- cabin (ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST)
- nonstop (true/false)
- max_price (number or null)
- sort (cheapest, fastest, recommended)

Rules:
- Use null if information is missing
- If a city is mentioned, infer the main airport
- Dates must be ISO format (YYYY-MM-DD)
- Only return JSON
- No explanation text

User request:
\"\"\"{user_text}\"\"\"
"""

    response = model.generate_content(prompt)

    # DEBUG (keep for now)
    print("RAW GEMINI OUTPUT:\n", response.text)

    text = response.text.strip()

    # Remove markdown code fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except Exception as e:
        print("JSON PARSE ERROR:", e)
        print("TEXT WAS:", text)
        return None


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

def parse_ai_query(text: str) -> dict:
    # simple wrapper so your route can call it
    parsed = parse_ai_flight_request(text or "")
    if not parsed:
        return {
            "origin": "",
            "destination": "",
            "depart_date": "",
            "return_date": "",
            "passengers": 1,
            "cabin": "ECONOMY",
            "nonstop": False,
            "sort": "recommended",
            "max_price": None,
            "raw_text": (text or "").strip()
        }
    return parsed

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/search", methods=["POST"])
def search():
    mode = request.form.get("mode")  # "standard" or "ai"

    if mode == "ai":
        ai_text = request.form.get("ai_text", "").strip()

        # Prefer Gemini parsing; fallback to your placeholder parser if Gemini fails
        params = None
        try:
            # If you implemented Gemini as: parse_ai_flight_request(text) -> dict
            params = parse_ai_flight_request(ai_text)
        except Exception:
            params = None

        if not params:
            # fallback to your existing placeholder heuristic
            params = parse_ai_query(ai_text)

        # Ensure raw_text exists for UI/debugging consistency
        params["raw_text"] = ai_text

        # Normalize a few fields (optional but helps prevent weird casing)
        if params.get("origin"):
            params["origin"] = params["origin"].strip().upper()
        if params.get("destination"):
            params["destination"] = params["destination"].strip().upper()

        # Defaults (so missing fields don’t crash your app)
        params["passengers"] = int(params.get("passengers") or 1)
        params["cabin"] = params.get("cabin") or "ECONOMY"
        params["nonstop"] = bool(params.get("nonstop") or False)
        params["sort"] = params.get("sort") or "recommended"

    else:
        # standard form (your current behavior)
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
