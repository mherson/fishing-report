#!/usr/bin/env python3
"""
Daily Wilmington, NC Fishing Report via Twilio SMS
Fetches tide, weather, wind, and suggests bait based on conditions.
Run this script daily via cron or a scheduler.
"""

import os
import requests
from datetime import datetime, timedelta
from pathlib import Path
from twilio.rest import Client

# ─── AUTO-LOAD .env (works on Mac, Windows, Linux — no terminal setup needed) ──
def load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print(f"⚠️  No .env file found at {env_path}")
        print("    Copy .env.example to .env and fill in your credentials.")
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

load_env()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN",  "")
TWILIO_FROM        = os.environ.get("TWILIO_FROM",        "")  # Your Twilio number
TWILIO_TO          = os.environ.get("TWILIO_TO",          "")  # Your cell number

# NOAA station 8658120 = Wilmington, NC
NOAA_STATION       = "8658120"
RAPIDAPI_KEY       = os.environ.get("RAPIDAPI_KEY", "fbda157c91msh5bd3e3971185d6ap126d65jsnc762429e685b")

# Open-Meteo (free, no key needed) for Wilmington coords
WILMINGTON_LAT     = 34.2257
WILMINGTON_LON     = -77.9447
# ───────────────────────────────────────────────────────────────────────────────


def get_tide_data():
    """Fetch today's hi/lo tides from NOAA via RapidAPI."""
    today    = datetime.now().strftime("%Y%m%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")

    url = f"https://noaa-tides.p.rapidapi.com/predictions"
    params = {
        "station":    NOAA_STATION,
        "begin_date": today,
        "end_date":   tomorrow,
        "datum":      "MLLW",
        "time_zone":  "lst_ldt",
        "interval":   "hilo",
        "units":      "english",
    }
    headers = {
        "x-rapidapi-host": "noaa-tides.p.rapidapi.com",
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "Content-Type":    "application/json",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json().get("predictions", [])


def fmt_time(t_str):
    """Convert '2024-03-15 06:30' to '6:30 AM'."""
    dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M")
    return dt.strftime("%-I:%M %p")


def get_weather():
    """Fetch today's weather from Open-Meteo (free, no API key needed)."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":   WILMINGTON_LAT,
        "longitude":  WILMINGTON_LON,
        "daily": [
            "temperature_2m_max",
            "temperature_2m_min",
            "windspeed_10m_max",
            "winddirection_10m_dominant",
            "precipitation_sum",
            "weathercode",
        ],
        "current_weather": True,
        "temperature_unit": "fahrenheit",
        "windspeed_unit":   "mph",
        "timezone":         "America/New_York",
        "forecast_days":    1,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def wind_direction(degrees):
    dirs = ["N","NE","E","SE","S","SW","W","NW"]
    return dirs[round(degrees / 45) % 8]


def weather_emoji(code):
    """WMO weather code to simple emoji."""
    if code == 0:            return "☀️"
    if code in (1, 2, 3):   return "⛅"
    if code in range(51,68): return "🌧️"
    if code in range(80,83): return "🌦️"
    if code in range(95,100):return "⛈️"
    return "🌥️"


def suggest_bait(hilos, wind_mph, temp_f, month):
    """
    Pick bait suggestions based on:
    - Dominant tide phase at prime morning window
    - Wind speed (affects surface vs bottom fishing)
    - Water season (month)
    """
    # Determine if morning tide is incoming or outgoing
    morning_hilos = [h for h in hilos if "06" <= h["t"].split(" ")[1] <= "12:00"]
    incoming = any(h["type"] == "H" for h in morning_hilos)

    # Season
    spring  = month in (3, 4, 5)
    summer  = month in (6, 7, 8)
    fall    = month in (9, 10, 11)
    winter  = month in (12, 1, 2)

    baits = []

    if incoming:
        baits.append("Live shrimp (under a popping cork near grass edges)")
        baits.append("Gulp! Swimming mullet (chartreuse or white)")
        if spring or fall:
            baits.append("Live finger mullet for red drum")
    else:
        baits.append("Cut mullet or menhaden (bottom rig for flounder & drum)")
        baits.append("Berkley Gulp! Jerkshad on 1/4 oz jighead")
        if summer:
            baits.append("Fiddler crabs for sheepshead around structure")

    if wind_mph > 15:
        baits.append("Heavier 1/2 oz jigs or bottom rigs to hold depth in wind")
    elif wind_mph < 8:
        baits.append("Topwater (Spook Jr. or Skitter Walk) during low-light windows")

    if winter:
        baits.append("Slow-rolled Z-Man Finesse ShadZ near warm water discharge")

    return baits[:3]  # Keep SMS concise — top 3 suggestions


def best_window(hilos):
    """Find the best 2-hr window before the first high tide."""
    today = datetime.now().strftime("%Y-%m-%d")
    today_highs = [h for h in hilos if h["type"] == "H" and today in h["t"]]
    if not today_highs:
        return "Check tides — no high tide today"
    first_high = datetime.strptime(today_highs[0]["t"], "%Y-%m-%d %H:%M")
    window_start = first_high - timedelta(hours=2)
    return f"{window_start.strftime('%-I:%M %p')} – {first_high.strftime('%-I:%M %p')}"


def build_message(hilos, weather):
    daily   = weather.get("daily", {})
    current = weather.get("current_weather", {})

    temp_high = round(daily.get("temperature_2m_max", [0])[0])
    temp_low  = round(daily.get("temperature_2m_min", [0])[0])
    wind_mph  = round(daily.get("windspeed_10m_max",  [0])[0])
    wind_deg  = daily.get("winddirection_10m_dominant", [0])[0]
    precip    = round(daily.get("precipitation_sum",  [0])[0], 2)
    wcode     = daily.get("weathercode", [0])[0]
    month     = datetime.now().month

    # Format tides
    today = datetime.now().strftime("%Y-%m-%d")
    today_hilos = [h for h in hilos if today in h["t"]]
    tide_lines = []
    for h in today_hilos:
        label = "High" if h["type"] == "H" else "Low"
        tide_lines.append(f"  {label}: {fmt_time(h['t'])} ({float(h['v']):.1f} ft)")

    tides_str = "\n".join(tide_lines) if tide_lines else "  No tide data"

    # Bait suggestions
    baits = suggest_bait(today_hilos, wind_mph, temp_high, month)
    bait_str = "\n".join(f"  • {b}" for b in baits)

    # Best window
    window = best_window(today_hilos)

    # Condition rating
    if wind_mph <= 12 and precip == 0:
        rating = "🟢 Great day to fish!"
    elif wind_mph <= 20 and precip < 0.1:
        rating = "🟡 Decent — fishable with prep"
    else:
        rating = "🔴 Rough — fish structure or stay home"

    date_str = datetime.now().strftime("%a, %b %-d")

    msg = f"""🎣 Wilmington Fishing Report — {date_str}

{weather_emoji(wcode)} Weather: {temp_high}°F high / {temp_low}°F low
💨 Wind: {wind_mph} mph {wind_direction(wind_deg)}
🌧️ Rain: {precip}" expected
{rating}

🌊 Tides (NOAA):
{tides_str}

⏰ Best window: {window}

🪱 Suggested bait:
{bait_str}

Good luck out there! 🐟"""

    return msg


def send_sms(body):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    message = client.messages.create(
        body=body,
        from_=TWILIO_FROM,
        to=TWILIO_TO,
    )
    print(f"SMS sent! SID: {message.sid}")
    return message.sid


def main():
    print(f"[{datetime.now()}] Fetching fishing report for Wilmington, NC...")

    try:
        hilos   = get_tide_data()
        weather = get_weather()
        message = build_message(hilos, weather)

        print("\n── Message preview ──────────────────────")
        print(message)
        print("─────────────────────────────────────────\n")

        send_sms(message)

    except Exception as e:
        print(f"ERROR: {e}")
        raise


if __name__ == "__main__":
    main()
