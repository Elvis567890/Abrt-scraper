
# dependency bootstrap (to avoid ModuleNotFoundError in bare environments) ---
def _ensure_dependencies():
    import importlib
    missing = []
    for mod in ["requests", "bs4", "playwright"]:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)

_ensure_dependencies()
# ------------------------------------------------------------------------------

import json
import os
import re
import urllib.request
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SPORTYBET_API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"
CHAMPIONBET_API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
BETIKA_API = "https://api-ug.betika.com/v1/uo/matches?page=1&limit=10&tab=&sub_type_id=1,186,340&sport_id=3&sort_id=1&period_id=-1&esports=false"

HISTORY_FILE = "arbitrage_history.json"
FRESHNESS_FILE = "odds_freshness.json"

STAKE = 100000
STALE_ODDS_HOURS = 3


def normalize(name):
    name = (name or "").lower().strip()
    name = re.sub(
        r"\b(fc|sc|cf|ac|united|city|sports|club|utd|football|soccer|women|men|u21|u23)\b",
        "",
    )
    name = re.sub(r"[\s\-]+", " ", name)
    return name.strip()


def fetch_json(url):
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_sportybet_odds():
    data = fetch_json(SPORTYBET_API)
    matches = []
    for item in data.get("matches", []):
        teams = item.get("teams")
        odds = item.get("odds")
        if teams and odds:
            matches.append({"match": teams, "bookmaker": "SportyBet", "odds": odds})
    return matches


def fetch_championbet_odds():
    data = fetch_json(CHAMPIONBET_API)
    matches = []
    for item in data.get("data", []):
        teams = f"{item.get('homeTeam')} vs {item.get('awayTeam')}"
        odds = {
            "home": item.get("homeOdd"),
            "draw": item.get("drawOdd"),
            "away": item.get("awayOdd"),
        }
        if all(odds.values()):
            matches.append({"match": teams, "bookmaker": "ChampionBet", "odds": odds})
    return matches


def fetch_betika_odds():
    data = fetch_json(BETIKA_API)
    matches = []
    for item in data.get("data", []):
        teams = f"{item.get('home_team')} vs {item.get('away_team')}"
        odds = {
            "home": item.get("home_win"),
            "draw": item.get("draw"),
            "away": item.get("away_win"),
        }
        if all(odds.values()):
            matches.append({"match": teams, "bookmaker": "Betika", "odds": odds})
    return matches


def fetch_all_odds():
    all_matches = []
    try:
        all_matches.extend(fetch_sportybet_odds())
    except Exception as e:
        print(f"Error fetching SportyBet odds: {e}")
    try:
        all_matches.extend(fetch_championbet_odds())
    except Exception as e:
        print(f"Error fetching ChampionBet odds: {e}")
    try:
        all_matches.extend(fetch_betika_odds())
    except Exception as e:
        print(f"Error fetching Betika odds: {e}")
    return all_matches


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r") as f:
        return json.load(f)


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def load_freshness():
    if not os.path.exists(FRESHNESS_FILE):
        return {}
    with open(FRESHNESS_FILE, "r") as f:
        return json.load(f)


def save_freshness(freshness):
    with open(FRESHNESS_FILE, "w") as f:
        json.dump(freshness, f, indent=2)


def is_stale(timestamp_str, hours=STALE_ODDS_HOURS):
    if not timestamp_str:
        return True
    timestamp = datetime.fromisoformat(timestamp_str)
    return datetime.now(timezone.utc) - timestamp > timedelta(hours=hours)


def update_freshness(all_odds):
    freshness = load_freshness()
    now_iso = datetime.now(timezone.utc).isoformat()
    for match in all_odds:
        key = f"{match['bookmaker']}::{match['match']}"
        freshness[key] = now_iso
    save_freshness(freshness)


def filter_stale_odds(all_odds):
    freshness = load_freshness()
    now = datetime.now(timezone.utc)
    fresh_odds = []
    for match in all_odds:
        key = f"{match['bookmaker']}::{match['match']}"
        ts = freshness.get(key)
        if not ts:
            fresh_odds.append(match)
            continue
        ts_dt = datetime.fromisoformat(ts)
        if now - ts_dt <= timedelta(hours=STALE_ODDS_HOURS):
            fresh_odds.append(match)
    return fresh_odds


def find_arbitrage(all_odds):
    opportunities = {}

    grouped_matches = {}
    for data in all_odds:
        match_name = data["match"]
        bookmaker = data["bookmaker"]
        odds = data["odds"]

        norm_match = normalize(match_name)
        if norm_match not in grouped_matches:
            grouped_matches[norm_match] = {}
        grouped_matches[norm_match][bookmaker] = {"raw_name": match_name, "odds": odds}

    for norm_match, bookmakers in grouped_matches.items():
        if len(bookmakers) < 2:
            continue

        best_odds = {}
        best_bookmakers = {}

        for bookmaker_name, info in bookmakers.items():
            odds = info["odds"]
            for outcome, odd in odds.items():
                if odd is None:
                    continue
                if outcome not in best_odds or odd > best_odds[outcome]:
                    best_odds[outcome] = odd
                    best_bookmakers[outcome] = bookmaker_name

        if len(best_odds) < 2:
            continue

        implied_prob_sum = sum(1 / odd for odd in best_odds.values())

        if implied_prob_sum < 1:
            profit_percent = (1 - implied_prob_sum) * 100
            stake_distribution = {}
            total_stake = STAKE
            for outcome, odd in best_odds.items():
                stake_distribution[outcome] = (total_stake / odd) / implied_prob_sum

            any_book = next(iter(bookmakers.values()))
            original_name = any_book["raw_name"]

            opportunities[norm_match] = {
                "match": original_name,
                "best_odds": best_odds,
                "best_bookmakers": best_bookmakers,
                "profit_percent": profit_percent,
                "stake_distribution": stake_distribution,
                "status": "not yet valid",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_checked": datetime.now(timezone.utc).isoformat(),
            }

    return opportunities


def refresh_opportunities(new_opportunities):
    history = load_history()
    existing = {opp["match"]: opp for opp in history}

    for _, new_opp in new_opportunities.items():
        match_name = new_opp["match"]
        if match_name in existing:
            opp = existing[match_name]
            opp["last_checked"] = datetime.now(timezone.utc).isoformat()
            opp["status"] = "valid" if new_opp["profit_percent"] >= 0 else "not yet valid"
            opp["best_odds"] = new_opp["best_odds"]
            opp["best_bookmakers"] = new_opp["best_bookmakers"]
            opp["profit_percent"] = new_opp["profit_percent"]
            opp["stake_distribution"] = new_opp["stake_distribution"]
        else:
            new_opp["status"] = "valid" if new_opp["profit_percent"] >= 0 else "not yet valid"
            new_opp["created_at"] = datetime.now(timezone.utc).isoformat()
            new_opp["last_checked"] = datetime.now(timezone.utc).isoformat()
            existing[match_name] = new_opp

    filtered = [opp for opp in existing.values() if not is_stale(opp["last_checked"])]

    save_history(filtered)
    return filtered


# ===== AIMLAPI SUMMARY HELPER (AI API) =====
AIMLAPI_API_KEY = os.environ.get("AIMLAPI_API_KEY")
AIMLAPI_BASE_URL = "https://api.aimlapi.com/v1"  # base chat endpoint [web:732][web:772]

def summarize_opportunities_with_aimlapi(opps):
    """
    Use AIMLAPI to generate a human-readable summary of current opportunities.
    Does not modify any data; only prints a summary if a key is present.
    """
    if not AIMLAPI_API_KEY:
        return
    if not opps:
        return

    top_opps = sorted(opps, key=lambda o: o.get("profit_percent", 0), reverse=True)[:5]

    lines = ["Here are some arbitrage opportunities in JSON form:", ""]
    for opp in top_opps:
        lines.append(json.dumps({
            "match": opp.get("match"),
            "profit_percent": opp.get("profit_percent"),
            "best_bookmakers": opp.get("best_bookmakers"),
            "best_odds": opp.get("best_odds"),
        }, ensure_ascii=False))
    lines.append("")
    lines.append(
        "In at most 8 sentences, explain these opportunities in simple language, "
        "highlighting the match, expected profit %, and which bookmakers to use."
    )
    prompt = "\n".join(lines)

    url = f"{AIMLAPI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {AIMLAPI_API_KEY}",  # auth header per docs [web:735]
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4o-mini",  # example supported model id [web:769][web:754]
        "messages": [
            {"role": "system", "content": "You are an expert sports betting assistant."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 600,
        "temperature": 0.4,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)  # [web:735][web:750]
        resp.raise_for_status()
        data = resp.json()
        summary = data["choices"][0]["message"]["content"]
        print("\n=== AIMLAPI Summary of Current Arbitrage Opportunities ===\n")
        print(summary)
    except Exception as e:
        print("Error calling AIMLAPI for summary:", e)
# ===== END AIMLAPI SUMMARY HELPER =====


def main():
    all_odds = fetch_all_odds()
    update_freshness(all_odds)
    fresh_odds = filter_stale_odds(all_odds)
    new_opps = find_arbitrage(fresh_odds)
    updated_history = refresh_opportunities(new_opps)

    print("Arbitrage opportunities updated:", len(updated_history))

    # Call AIMLAPI to summarize, if AIMLAPI_API_KEY is set
    summarize_opportunities_with_aimlapi(updated_history)


if __name__ == "__main__":
    main()
