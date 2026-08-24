#!/usr/bin/env python3
"""
FULL ARBITRAGE SCANNER FOR UGANDAN BOOKMAKERS
- Includes: GSB, Bangbet, Betmaster, ChampionBet, AbaBet, Fortebet, SportyBet, Melbet, 1xBet, BongoBongo, BetPawa (placeholder)
- 15% withholding tax on profit
- Multiple markets: 1x2, Over/Under, Asian Handicap, BTTS, Double Chance
- History & Telegram alerts
- Continuous polling
"""

import requests
import json
import re
import time
import os
from datetime import datetime, timedelta
from collections import defaultdict
from itertools import combinations, permutations
from copy import deepcopy

# ---------- Configuration ----------
POLL_INTERVAL = 10          # seconds between scans
TAX_RATE = 0.15             # Ugandan withholding tax on net profit
MAX_PROFIT = 60.0           # reject if raw profit > 60% (likely error)
STAKE = 100000              # total stake per arbitrage (UGX)
HISTORY_FILE = "arb_history.json"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# API endpoints
SPORTYBET_API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"  # likely outdated
CHAMPIONBET_API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
CHAMPIONBET_MATCH_API = "https://www.championbet.ug/restapi/offer/en/match/{match_id}?annex=13&mobileVersion=2.47.4.3&locale=en"
GSB_API = "https://gsb.ug/services/evapi/event/GetEvents"
BANGBET_API = "https://bet-api.bangbet.com/api/bet/match/list"
BETMASTER_API = "https://betmasterug.com/Sports.aspx/GetSportMarkets"
BETPAWA_API = "https://www.betpawa.ug/api/sportsbook/v1/combo-cards/list"
BONGOBONGO_API = "https://www.bongobongo.ug/api/_internal/sportsbook/v1/sport/v0/line/tournaments"
MELBET_API = "https://melbet-424658.top/service-api/LineFeed/GetTopGamesStatZip"
ONEXBET_API = "https://1x-bet.mobi/service-api/main-live-feed/v3/games1x2"

# ---------- Helper Functions ----------
def normalize(name):
    name = (name or "").lower().strip()
    name = re.sub(r"\b(rovers|rvs)\b", "rvs", name)
    name = re.sub(r"\b(united|utd)\b", "utd", name)
    name = re.sub(r"\b(fc|sc|cf|ac|city|sports|club|football|soccer|women|men|u21|u23)\b", "", name)
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def teams_match(name1, name2):
    n1 = normalize(name1)
    n2 = normalize(name2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    if len(n1) > 3 and len(n2) > 3:
        if n1 in n2 or n2 in n1:
            return True
        w1 = n1.split()[0] if n1.split() else ""
        w2 = n2.split()[0] if n2.split() else ""
        if len(w1) > 4 and w1 == w2:
            return True
    return False

def clean_odd(v, min_odd=1.01, max_odd=50.0):
    try:
        if v is None:
            return None
        v = float(v)
        if min_odd <= v <= max_odd:
            return v
    except:
        pass
    return None

def build_match_record(home_team, away_team, bookmaker, home, draw, away, sport="Football", competition="", market_type="1x2", market_specifier=""):
    base_key = f"{normalize(home_team)} vs {normalize(away_team)}"
    if market_type == "Over/Under 2.5":
        match_key = f"{base_key} | O/U 2.5"
    elif market_type == "Asian Handicap":
        match_key = f"{base_key} | AH"
    elif market_type == "Double Chance":
        match_key = f"{base_key} | DC {market_specifier}"
    elif market_type == "BTTS":
        match_key = f"{base_key} | BTTS"
    else:
        match_key = base_key

    return {
        "match": f"{home_team} vs {away_team}",
        "home_team": home_team,
        "away_team": away_team,
        "match_key": match_key,
        "bookmaker": bookmaker,
        "competition": competition,
        "home": home,
        "draw": draw,
        "away": away,
        "sport": sport,
        "market_type": market_type,
        "market_specifier": market_specifier
    }

# ---------- History Management ----------
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return {}

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def is_new_arb(key, history):
    if key not in history:
        history[key] = {"first_seen": datetime.now().isoformat(), "last_seen": datetime.now().isoformat(), "count": 1}
        return True
    else:
        history[key]["last_seen"] = datetime.now().isoformat()
        history[key]["count"] += 1
        return False

# ---------- Telegram Alert ----------
def send_telegram_alert(match_info, profit):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    message = f"⚽ ARB FOUND: {match_info}\nProfit after tax: {profit}%"
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=5)
    except:
        pass

# ---------- Scrapers ----------
# 1. ChampionBet
def championbet_extract_1x2_from_betmap(bet_map):
    bet_map = bet_map or {}
    def pick_odd(market_keys):
        for k in market_keys:
            market = bet_map.get(str(k), {}) or {}
            if not isinstance(market, dict): continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None: return odd
        return None
    return pick_odd([1, 4, 7]), pick_odd([2, 5, 8]), pick_odd([3, 6, 9])

def championbet_extract_ou_from_betmap(bet_map):
    bet_map = bet_map or {}
    def pick_odd(market_keys):
        for k in market_keys:
            market = bet_map.get(str(k), {}) or {}
            if not isinstance(market, dict): continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None: return odd
        return None
    return pick_odd([51, 21]), pick_odd([52, 22])

def championbet_extract_ah_dc_btts_from_betmap(bet_map):
    bet_map = bet_map or {}
    ah_odds, dc_odds, btts_odds = {}, {}, {}
    def get_odds(market_keys):
        odds_dict = {}
        for k in market_keys:
            market = bet_map.get(str(k), {}) or {}
            if not isinstance(market, dict): continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None:
                        odds_dict[k] = odd
        return odds_dict

    ah_odds = get_odds([5, 6, 7, 8])
    dc_odds = get_odds([20, 21, 22])
    btts_odds = get_odds([19, 20])
    return ah_odds, dc_odds, btts_odds

def scrape_championbet():
    odds = []
    try:
        print("Fetching ChampionBet...")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Referer": "https://www.championbet.ug/mob/",
        }
        req = urllib.request.Request(CHAMPIONBET_API, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            top_data = json.loads(resp.read().decode())

        matches = top_data.get("esMatches", []) if isinstance(top_data, dict) else []
        print(f"ChampionBet: {len(matches)} matches in top list")
        count = 0
        for m in matches:
            try:
                sport_token = str(m.get("sportToken", ""))
                if "Soccer" not in sport_token: continue
                match_id = m.get("id")
                if not match_id: continue
                home_team = m.get("home") or ""
                away_team = m.get("away") or ""
                if not home_team or not away_team: continue

                match_url = CHAMPIONBET_MATCH_API.format(match_id=match_id)
                match_req = urllib.request.Request(match_url, headers=headers)
                with urllib.request.urlopen(match_req, timeout=30) as r2:
                    match_data = json.loads(r2.read().decode())
                bet_map = match_data.get("betMap", {}) if isinstance(match_data, dict) else {}

                h, d, a = championbet_extract_1x2_from_betmap(bet_map)
                if h and a:
                    count += 1
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", h, d, a, competition=m.get("leagueName", ""), market_type="1x2"))

                over, under = championbet_extract_ou_from_betmap(bet_map)
                if over and under:
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", over, under, None, market_type="Over/Under 2.5"))

                ah_odds, dc_odds, btts_odds = championbet_extract_ah_dc_btts_from_betmap(bet_map)
                if ah_odds.get(5) and ah_odds.get(6):
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", ah_odds[5], None, ah_odds[6], market_type="Asian Handicap", market_specifier="-1.5"))
                if ah_odds.get(7) and ah_odds.get(8):
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", ah_odds[7], None, ah_odds[8], market_type="Asian Handicap", market_specifier="-0.5"))
                if dc_odds.get(20): odds.append(build_match_record(home_team, away_team, "ChampionBet", dc_odds[20], None, None, market_type="Double Chance", market_specifier="1X"))
                if dc_odds.get(21): odds.append(build_match_record(home_team, away_team, "ChampionBet", None, None, dc_odds[21], market_type="Double Chance", market_specifier="X2"))
                if dc_odds.get(22): odds.append(build_match_record(home_team, away_team, "ChampionBet", dc_odds[22], None, None, market_type="Double Chance", market_specifier="12"))
                if btts_odds.get(19) and btts_odds.get(20):
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", btts_odds[19], None, btts_odds[20], market_type="BTTS"))

                time.sleep(0.2)
            except:
                continue
        print(f"ChampionBet: {count} matches extracted")
    except Exception as e:
        print(f"ChampionBet error: {e}")
    return odds

# 2. AbaBet
def scrape_ababet():
    odds = []
    try:
        print("Fetching AbaBet...")
        url = "https://www.ababet.ug/soccer/match_result?mobile=1"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            print("AbaBet: no tables found")
            return odds

        for table in tables:
            first_row = table.find("tr")
            if not first_row: continue
            headers = [c.get_text(" ", strip=True) for c in first_row.find_all(["th", "td"])]
            if "Home" not in headers or "Away" not in headers: continue
            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 5: continue
                row = dict(zip(headers, cells[:len(headers)]))
                home, away = row.get("Home"), row.get("Away")
                if not home or not away or home == "-" or away == "-": continue

                h = row.get("1"); d = row.get("X"); a = row.get("2")
                if h and a:
                    odds.append(build_match_record(home, away, "AbaBet", h, d, a, competition=row.get("League", ""), market_type="1x2"))

                over = row.get("Over"); under = row.get("Under")
                if over and under:
                    odds.append(build_match_record(home, away, "AbaBet", over, under, None, market_type="Over/Under 2.5"))

        print(f"AbaBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"AbaBet error: {e}")
    return odds

# 3. Fortebet
def scrape_fortebet():
    odds = []
    try:
        print("Fetching Fortebet API...")
        url = "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://desktop.fortebet.ug/prematch/landing"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        inner = data.get("data", {})
        events = inner.get("event", {})
        markets = inner.get("markets", {})
        competitors = inner.get("competitors", {})
        event_markets = {}
        for _, market in markets.items():
            event_markets.setdefault(str(market.get("eventId", "")), []).append(market)

        count = 0
        for eid, event in events.items():
            try:
                comps = event.get("competitors", [])
                if len(comps) < 2: continue
                home = competitors.get(str(comps[0]), {}).get("name", "")
                away = competitors.get(str(comps[1]), {}).get("name", "")
                if not home or not away: continue
                h = d = a = over = under = None
                ah_home = ah_away = None
                dc_home = dc_away = None
                btts_yes = btts_no = None

                for market in event_markets.get(eid, []):
                    mid = market.get("marketId")
                    if mid == 1:
                        odd_list = []
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                odd_list.append((v.get("outcomeId", 0), clean_odd(v["odds"])))
                        odd_list = [(i, o) for i, o in odd_list if o is not None]
                        odd_list.sort(key=lambda x: x[0])
                        if len(odd_list) >= 3:
                            h, d, a = odd_list[0][1], odd_list[1][1], odd_list[2][1]
                        elif len(odd_list) == 2:
                            h, a = odd_list[0][1], odd_list[1][1]
                    elif mid == 5:
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1: over = clean_odd(v["odds"])
                                elif oid == 2: under = clean_odd(v["odds"])
                    elif mid == 2:
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1: ah_home = clean_odd(v["odds"])
                                elif oid == 2: ah_away = clean_odd(v["odds"])
                    elif mid == 8:
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1: dc_home = clean_odd(v["odds"])
                                elif oid == 3: dc_away = clean_odd(v["odds"])
                    elif mid == 12:
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1: btts_yes = clean_odd(v["odds"])
                                elif oid == 2: btts_no = clean_odd(v["odds"])

                if h and a:
                    sport_name = "Netball" if d is None else "Football"
                    ev_sport = (event.get("sportName") or event.get("sport") or "").lower()
                    if "basketball" in ev_sport: sport_name = "Basketball"
                    elif "tennis" in ev_sport: sport_name = "Tennis"
                    count += 1
                    odds.append(build_match_record(home, away, "Fortebet", h, d, a, sport=sport_name, market_type="1x2"))

                if over and under:
                    odds.append(build_match_record(home, away, "Fortebet", over, under, None, sport="Football", market_type="Over/Under 2.5"))

                if ah_home and ah_away:
                    odds.append(build_match_record(home, away, "Fortebet", ah_home, None, ah_away, sport="Football", market_type="Asian Handicap", market_specifier="-0.5"))

                if dc_home: odds.append(build_match_record(home, away, "Fortebet", dc_home, None, None, sport="Football", market_type="Double Chance", market_specifier="1X"))
                if dc_away: odds.append(build_match_record(home, away, "Fortebet", None, None, dc_away, sport="Football", market_type="Double Chance", market_specifier="12"))

                if btts_yes and btts_no:
                    odds.append(build_match_record(home, away, "Fortebet", btts_yes, None, btts_no, sport="Football", market_type="BTTS"))

            except: continue
        print(f"Fortebet: {count} matches extracted")
    except Exception as e:
        print(f"Fortebet error: {e}")
    return odds

# 4. SportyBet (may fail)
def scrape_sportybet():
    odds = []
    try:
        print("Fetching SportyBet...")
        req = urllib.request.Request(SPORTYBET_API, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, list):
            for event in data:
                try:
                    home, away = event.get("home_team", ""), event.get("away_team", "")
                    if not home or not away: continue
                    sport = (event.get("sport") or "Football").strip()
                    h = clean_odd(event.get("home"))
                    d = clean_odd(event.get("draw"))
                    a = clean_odd(event.get("away"))
                    if h and a:
                        odds.append(build_match_record(home, away, "SportyBet", h, d, a, sport=sport, market_type="1x2"))
                    over = clean_odd(event.get("over_odd"))
                    under = clean_odd(event.get("under_odd"))
                    if over and under:
                        odds.append(build_match_record(home, away, "SportyBet", over, under, None, sport=sport, market_type="Over/Under 2.5"))
                except: continue
        print(f"SportyBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"SportyBet error: {e}")
    return odds

# 5. Melbet (may timeout)
def scrape_melbet():
    odds = []
    try:
        print("Fetching Melbet (GetTopGamesStatZip)...")
        base_url = "https://melbet-424658.top"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-mobile-project-id": "0",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": f"{base_url}/en/line/football",
        }

        top_url = f"{base_url}/service-api/LineFeed/GetTopGamesStatZip"
        top_params = {
            "lng": "en",
            "antisports": "66",
            "partner": "8"
        }
        top_req = requests.get(top_url, headers=headers, params=top_params, timeout=30)
        top_data = top_req.json()
        events = top_data.get("Value", [])
        print(f"Melbet: {len(events)} top games found")

        for event in events:
            try:
                home = event.get("O1", "")
                away = event.get("O2", "")
                if not home or not away:
                    continue
                if event.get("SI") != 1:
                    continue

                odds_map = {}
                for item in event.get("E", []):
                    t = item.get("T")
                    c = clean_odd(item.get("C"))
                    if c is None:
                        continue
                    p = item.get("P")
                    key = (t, p)
                    odds_map[key] = c

                home_odd = odds_map.get((1, None))
                draw_odd = odds_map.get((2, None))
                away_odd = odds_map.get((3, None))
                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "Melbet", home_odd, draw_odd, away_odd,
                                                   sport="Football", competition=event.get("L", ""), market_type="1x2"))

                over_25 = odds_map.get((11, None)) or odds_map.get((11, 2.5))
                under_25 = odds_map.get((12, None)) or odds_map.get((12, 2.5))
                if over_25 and under_25:
                    odds.append(build_match_record(home, away, "Melbet", over_25, under_25, None,
                                                   sport="Football", competition=event.get("L", ""), market_type="Over/Under 2.5"))

                ah_home_odds = {}
                ah_away_odds = {}
                for (t, p), c in odds_map.items():
                    if t in (7, 13, 3829):
                        if p is not None:
                            ah_home_odds[p] = c
                    elif t in (8, 14, 3830):
                        if p is not None:
                            ah_away_odds[p] = c
                for line in set(ah_home_odds.keys()) & set(ah_away_odds.keys()):
                    if line is None:
                        continue
                    odds.append(build_match_record(home, away, "Melbet", ah_home_odds[line], None, ah_away_odds[line],
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="Asian Handicap", market_specifier=str(line)))

                dc_1x = odds_map.get((180, None))
                dc_x2 = odds_map.get((182, None))
                dc_12 = odds_map.get((181, None))
                if dc_1x:
                    odds.append(build_match_record(home, away, "Melbet", dc_1x, None, None,
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="Double Chance", market_specifier="1X"))
                if dc_x2:
                    odds.append(build_match_record(home, away, "Melbet", None, None, dc_x2,
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="Double Chance", market_specifier="X2"))
                if dc_12:
                    odds.append(build_match_record(home, away, "Melbet", dc_12, None, None,
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="Double Chance", market_specifier="12"))

                btts_yes = odds_map.get((19, None))
                btts_no = odds_map.get((20, None))
                if btts_yes and btts_no:
                    odds.append(build_match_record(home, away, "Melbet", btts_yes, None, btts_no,
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="BTTS"))

            except Exception:
                continue

        print(f"Melbet: extracted {len(odds)} records")
    except Exception as e:
        print(f"Melbet error: {e}")
    return odds

# 6. 1xBet (may timeout)
def scrape_1xbet():
    odds = []
    try:
        print("Fetching 1xBet (main-live-feed/v3/games1x2)...")
        base_url = "https://1x-bet.mobi"
        url = f"{base_url}/service-api/main-live-feed/v3/games1x2"
        params = {
            "cfView": "3",
            "count": "50",
            "fcountry": "191",
            "gr": "455",
            "grMode": "4",
            "lng": "en",
            "ref": "1"
        }
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-mobile-project-id": "0",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": f"{base_url}/en/line/football",
        }
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if not isinstance(data, list):
            print("1xBet: API returned non-list")
            return odds

        for event in data:
            try:
                sport_id = event.get("sport", {}).get("id", 0)
                if sport_id != 1:   # Football only
                    continue
                home = event.get("opponent1", {}).get("fullName", "")
                away = event.get("opponent2", {}).get("fullName", "")
                if not home or not away:
                    continue
                competition = event.get("liga", {}).get("name", "")

                odds_map = {}
                for group in event.get("eventGroups", []):
                    gid = group.get("groupId")
                    for event_list in group.get("events", []):
                        for item in event_list:
                            t = item.get("type")
                            c = clean_odd(item.get("cf"))
                            if c is None:
                                continue
                            p = item.get("parameter")
                            odds_map[(gid, t, p)] = c

                home_odd = odds_map.get((1, 1, None))
                draw_odd = odds_map.get((1, 2, None))
                away_odd = odds_map.get((1, 3, None))
                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "1xBet", home_odd, draw_odd, away_odd,
                                                   sport="Football", competition=competition, market_type="1x2"))

                ou_lines = {}
                for (gid, t, p), c in odds_map.items():
                    if gid == 17 and t == 9:
                        ou_lines.setdefault(p, {})["over"] = c
                    elif gid == 17 and t == 10:
                        ou_lines.setdefault(p, {})["under"] = c
                for line, vals in ou_lines.items():
                    if line is None:
                        continue
                    over = vals.get("over")
                    under = vals.get("under")
                    if over and under:
                        odds.append(build_match_record(home, away, "1xBet", over, under, None,
                                                       sport="Football", competition=competition,
                                                       market_type="Over/Under", market_specifier=str(line)))
                        if line == 2.5:
                            break

                ah_home = {}
                ah_away = {}
                for (gid, t, p), c in odds_map.items():
                    if gid == 2 and t == 7:
                        ah_home[p] = c
                    elif gid == 2 and t == 8:
                        ah_away[p] = c
                for line in set(ah_home.keys()) & set(ah_away.keys()):
                    if line is None:
                        continue
                    odds.append(build_match_record(home, away, "1xBet", ah_home[line], None, ah_away[line],
                                                   sport="Football", competition=competition,
                                                   market_type="Asian Handicap", market_specifier=str(line)))

                dc_1x = odds_map.get((8, 4, None)) or odds_map.get((19, 180, None))
                dc_x2 = odds_map.get((8, 5, None)) or odds_map.get((19, 182, None))
                dc_12 = odds_map.get((8, 6, None)) or odds_map.get((19, 181, None))
                if dc_1x:
                    odds.append(build_match_record(home, away, "1xBet", dc_1x, None, None,
                                                   sport="Football", competition=competition,
                                                   market_type="Double Chance", market_specifier="1X"))
                if dc_x2:
                    odds.append(build_match_record(home, away, "1xBet", None, None, dc_x2,
                                                   sport="Football", competition=competition,
                                                   market_type="Double Chance", market_specifier="X2"))
                if dc_12:
                    odds.append(build_match_record(home, away, "1xBet", dc_12, None, None,
                                                   sport="Football", competition=competition,
                                                   market_type="Double Chance", market_specifier="12"))

            except Exception:
                continue

        print(f"1xBet: extracted {len(odds)} records")
    except Exception as e:
        print(f"1xBet error: {e}")
    return odds

# 7. BongoBongo (placeholder)
def scrape_bongobongo():
    odds = []
    try:
        print("Fetching BongoBongo...")
        # Placeholder - implement when real endpoint available
        print("BongoBongo: placeholder - no odds extracted")
    except Exception as e:
        print(f"BongoBongo error: {e}")
    return odds

# 8. GSB (full parser)
def fetch_gsb(sport_id=31, limit=50):
    url = GSB_API
    headers = {
        "BrandId": "112",
        "ChannelId": "4",
        "Language": "en-US",
        "Terminal": "gsb.ug"
    }
    params = {
        "betTypeIds": "-1",
        "take": limit,
        "statusId": "0",
        "eventTypeId": "0",
        "toDate": "2026-08-25T00:00:00.000Z",
        "skip": "0",
        "sportTypeIds": str(sport_id)
    }
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

def parse_gsb(data):
    events = []
    for ev in data.get("data", []):
        if "Simulated" in ev.get("cn", ""):
            continue
        league = ev.get("ln", "")
        home = ev.get("h", "")
        away = ev.get("a", "")
        time_str = ev.get("gt", "")
        # 1x2
        market = next((b for b in ev.get("bts", []) if b.get("n") == "FT 1X2"), None)
        if market:
            odds = {odd["n"]: float(odd.get("p", 0)) for odd in market.get("odds", []) if odd["n"] in ("1", "X", "2")}
            if len(odds) == 3:
                events.append({"league": league, "home": home, "away": away, "time": time_str,
                               "odds": odds, "source": "gsb", "market_type": "1x2"})
        # Over/Under (2.5)
        market = next((b for b in ev.get("bts", []) if b.get("n") == "Under/Over"), None)
        if market:
            for odd in market.get("odds", []):
                if odd.get("l") == "2.5":
                    over = float(odd.get("p", 0)) if odd.get("n") == "over" else 0
                    under = float(odd.get("p", 0)) if odd.get("n") == "under" else 0
                    if over and under:
                        events.append({"league": league, "home": home, "away": away, "time": time_str,
                                       "odds": {"over": over, "under": under}, "source": "gsb", "market_type": "ou"})
                    break
        # Asian Handicap (any line)
        market = next((b for b in ev.get("bts", []) if b.get("n") == "Asian Handicap"), None)
        if market:
            home_odds = {}
            away_odds = {}
            for odd in market.get("odds", []):
                line = float(odd.get("l", 0))
                if odd.get("n") == "1":
                    home_odds[line] = float(odd.get("p", 0))
                elif odd.get("n") == "2":
                    away_odds[line] = float(odd.get("p", 0))
            for line, h_odd in home_odds.items():
                a_odd = away_odds.get(-line)
                if a_odd:
                    events.append({"league": league, "home": home, "away": away, "time": time_str,
                                   "odds": {"home": h_odd, "away": a_odd}, "source": "gsb",
                                   "market_type": "ah", "line": line})
                    break  # only one line per match
        # Double Chance
        market = next((b for b in ev.get("bts", []) if b.get("n") == "Double Chance"), None)
        if market:
            odds = {}
            for odd in market.get("odds", []):
                label = odd.get("n")
                if label in ("1X", "12", "X2"):
                    odds[label] = float(odd.get("p", 0))
            if len(odds) == 3:
                events.append({"league": league, "home": home, "away": away, "time": time_str,
                               "odds": odds, "source": "gsb", "market_type": "dc"})
        # BTTS
        market = next((b for b in ev.get("bts", []) if b.get("n") == "GG/NG"), None)
        if market:
            yes = next((float(o.get("p", 0)) for o in market.get("odds", []) if o.get("n") == "Yes"), None)
            no = next((float(o.get("p", 0)) for o in market.get("odds", []) if o.get("n") == "No"), None)
            if yes and no:
                events.append({"league": league, "home": home, "away": away, "time": time_str,
                               "odds": {"yes": yes, "no": no}, "source": "gsb", "market_type": "btts"})
    return events

# 9. BangBet (1x2 only)
def fetch_bangbet():
    url = BANGBET_API
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, text/plain, */*"
    }
    payload = {
        "sportId": "sr:sport:1",
        "groupIndex": 0,
        "tournamentId": "",
        "producer": 3,
        "position": 17,
        "beginTime": "",
        "highLight": True,
        "endTime": "",
        "showMarket": True,
        "timeZone": "+3",
        "page": 1,
        "sortType": 1,
        "pageSize": 50,
        "marketChildrenIndex": 0,
        "pageNo": 1,
        "minOdds": None,
        "maxOdds": None,
        "isUpMarket": True,
        "dataGroup": False
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

def parse_bangbet(data):
    events = []
    for ev in data.get("data", {}).get("data", []):
        league = ev.get("tournamentName", "")
        home = ev.get("homeTeamName", "")
        away = ev.get("awayTeamName", "")
        time_str = ev.get("scheduledDate", "")
        # 1x2
        for market_list in ev.get("marketList", []):
            for market in market_list.get("markets", []):
                if market.get("name") == "1x2":
                    odds = {}
                    for out in market.get("outcomes", []):
                        label = out.get("desc", "")
                        if "draw" in label.lower():
                            key = "X"
                        elif out.get("id") == "1":
                            key = "1"
                        elif out.get("id") == "3":
                            key = "2"
                        else:
                            continue
                        odds[key] = float(out.get("odds", 0))
                    if len(odds) == 3:
                        events.append({"league": league, "home": home, "away": away, "time": time_str,
                                       "odds": odds, "source": "bangbet", "market_type": "1x2"})
                    break
    return events

# 10. BetMaster (1x2 only for now)
def fetch_betmaster(sportid='1', isfeatured=1):
    url = BETMASTER_API
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest"
    }
    payload = {
        "sportid": sportid,
        "countryid": "",
        "leagueid": "",
        "isfeatured": isfeatured,
        "searchteam": 0,
        "filter": 100
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    matches = json.loads(data["d"])
    return matches

def parse_betmaster(matches):
    events = []
    for m in matches:
        league = m.get("LeagueName", "")
        home = m.get("hometeam", "")
        away = m.get("awayteam", "")
        time_str = m.get("starttime", "")
        i = 1
        while True:
            market_id_key = f"marketidmarket{i}"
            if market_id_key not in m:
                break
            market_name = m.get(f"marketnamemarket{i}", "")
            if market_name == "Match Winner" or m.get(market_id_key) == 1:
                try:
                    odds1 = float(m.get(f"outcomeodd1market{i}", "0"))
                    oddsX = float(m.get(f"outcomeodd2market{i}", "0"))
                    odds2 = float(m.get(f"outcomeodd3market{i}", "0"))
                    if odds1 and oddsX and odds2:
                        events.append({"league": league, "home": home, "away": away, "time": time_str,
                                       "odds": {"1": odds1, "X": oddsX, "2": odds2},
                                       "source": "betmaster", "market_type": "1x2"})
                    break
                except:
                    pass
            i += 1
    return events

# 11. BetPawa (placeholder, disabled)
def fetch_betpawa():
    return {}

def parse_betpawa(data):
    return []

# ---------- Arbitrage Detection ----------
def make_match_key(event):
    league = event.get("league", "").strip().lower()
    home = normalize(event.get("home", ""))
    away = normalize(event.get("away", ""))
    time_str = event.get("time", "")
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        rounded_min = 30 if dt.minute >= 30 else 0
        dt = dt.replace(minute=rounded_min, second=0, microsecond=0)
        time_key = dt.strftime("%Y-%m-%dT%H:%M")
    except:
        time_key = "unknown"
    market = event.get("market_type", "1x2")
    return (league, home, away, time_key, market)

def net_profit(profit_percent):
    return round(profit_percent * (1 - TAX_RATE), 2)

def arb_exists(odds_dict):
    """odds_dict: dict of {outcome: odd}. Returns net profit % or None."""
    if len(odds_dict) < 2:
        return None
    prob = sum(1 / odd for odd in odds_dict.values() if odd > 0)
    if prob < 1:
        raw_profit = (1 - prob) * 100
        if raw_profit > MAX_PROFIT:
            return None
        net = net_profit(raw_profit)
        if net > 0:
            return net
    return None

def scan():
    all_events = []
    history = load_history()

    # Scrape all sources
    try:
        print("Fetching ChampionBet...")
        all_events.extend(scrape_championbet())
    except Exception as e:
        print(f"ChampionBet error: {e}")
    time.sleep(1)

    try:
        print("Fetching AbaBet...")
        all_events.extend(scrape_ababet())
    except Exception as e:
        print(f"AbaBet error: {e}")
    time.sleep(1)

    try:
        print("Fetching Fortebet...")
        all_events.extend(scrape_fortebet())
    except Exception as e:
        print(f"Fortebet error: {e}")
    time.sleep(1)

    try:
        print("Fetching SportyBet...")
        all_events.extend(scrape_sportybet())
    except Exception as e:
        print(f"SportyBet error: {e}")
    time.sleep(1)

    try:
        print("Fetching Melbet...")
        all_events.extend(scrape_melbet())
    except Exception as e:
        print(f"Melbet error: {e}")
    time.sleep(1)

    try:
        print("Fetching 1xBet...")
        all_events.extend(scrape_1xbet())
    except Exception as e:
        print(f"1xBet error: {e}")
    time.sleep(1)

    try:
        print("Fetching BongoBongo...")
        all_events.extend(scrape_bongobongo())
    except Exception as e:
        print(f"BongoBongo error: {e}")
    time.sleep(1)

    try:
        print("Fetching GSB...")
        gsb_data = fetch_gsb()
        gsb_events = parse_gsb(gsb_data)
        all_events.extend(gsb_events)
        print(f"  GSB: {len(gsb_events)} events")
    except Exception as e:
        print(f"GSB error: {e}")
    time.sleep(1)

    try:
        print("Fetching Bangbet...")
        bang_data = fetch_bangbet()
        bang_events = parse_bangbet(bang_data)
        all_events.extend(bang_events)
        print(f"  Bangbet: {len(bang_events)} events")
    except Exception as e:
        print(f"Bangbet error: {e}")
    time.sleep(1)

    try:
        print("Fetching Betmaster...")
        bm_data = fetch_betmaster()
        bm_events = parse_betmaster(bm_data)
        all_events.extend(bm_events)
        print(f"  Betmaster: {len(bm_events)} events")
    except Exception as e:
        print(f"Betmaster error: {e}")
    time.sleep(1)

    # BetPawa is disabled (placeholder)
    # try:
    #     print("Fetching BetPawa...")
    #     bp_events = parse_betpawa(fetch_betpawa())
    #     all_events.extend(bp_events)
    # except Exception as e:
    #     print(f"BetPawa error: {e}")

    # Group events
    groups = defaultdict(list)
    for ev in all_events:
        key = make_match_key(ev)
        groups[key].append(ev)

    arbs_found = 0
    for key, ev_list in groups.items():
        if len(ev_list) < 2:
            continue
        league, home, away, time_key, market_type = key
        match_str = f"{home} vs {away} [{league}] @ {time_key}"

        # Process per market type
        if market_type == "1x2":
            best = {"1": 0, "X": 0, "2": 0}
            for ev in ev_list:
                for k in "1X2":
                    if k in ev["odds"]:
                        best[k] = max(best[k], ev["odds"][k])
            profit = arb_exists(best)
            if profit:
                arbs_found += 1
                print(f"\nARB FOUND (1x2): {match_str}")
                print(f"  Best odds: {best} | Net profit: {profit}%")
                for ev in ev_list:
                    print(f"    {ev['source']}: {ev['odds']}")
                if is_new_arb(match_str + "_1x2", history):
                    send_telegram_alert(match_str, profit)

        elif market_type == "ou":
            best = {"over": 0, "under": 0}
            for ev in ev_list:
                if "over" in ev["odds"]:
                    best["over"] = max(best["over"], ev["odds"]["over"])
                if "under" in ev["odds"]:
                    best["under"] = max(best["under"], ev["odds"]["under"])
            profit = arb_exists(best)
            if profit:
                arbs_found += 1
                print(f"\nARB FOUND (O/U): {match_str}")
                print(f"  Best odds: {best} | Net profit: {profit}%")
                for ev in ev_list:
                    print(f"    {ev['source']}: {ev['odds']}")
                if is_new_arb(match_str + "_ou", history):
                    send_telegram_alert(match_str, profit)

        elif market_type == "ah":
            # group by line
            line_groups = defaultdict(list)
            for ev in ev_list:
                line = ev.get("line")
                if line is not None:
                    line_groups[line].append(ev)
            for line, line_evs in line_groups.items():
                home_best = 0
                away_best = 0
                home_bookie = None
                away_bookie = None
                for ev in line_evs:
                    if "home" in ev["odds"] and ev["odds"]["home"] > home_best:
                        home_best = ev["odds"]["home"]
                        home_bookie = ev["source"]
                    if "away" in ev["odds"] and ev["odds"]["away"] > away_best:
                        away_best = ev["odds"]["away"]
                        away_bookie = ev["source"]
                if home_bookie != away_bookie and home_best and away_best:
                    prob = 1/home_best + 1/away_best
                    if prob < 1:
                        raw_profit = (1 - prob) * 100
                        if raw_profit <= MAX_PROFIT:
                            profit = net_profit(raw_profit)
                            if profit > 0:
                                arbs_found += 1
                                print(f"\nARB FOUND (AH {line}): {match_str}")
                                print(f"  Home: {home_best} ({home_bookie}), Away: {away_best} ({away_bookie})")
                                print(f"  Net profit: {profit}%")
                                if is_new_arb(match_str + f"_ah_{line}", history):
                                    send_telegram_alert(match_str, profit)

        elif market_type == "btts":
            best = {"yes": 0, "no": 0}
            for ev in ev_list:
                if "yes" in ev["odds"]:
                    best["yes"] = max(best["yes"], ev["odds"]["yes"])
                if "no" in ev["odds"]:
                    best["no"] = max(best["no"], ev["odds"]["no"])
            profit = arb_exists(best)
            if profit:
                arbs_found += 1
                print(f"\nARB FOUND (BTTS): {match_str}")
                print(f"  Best odds: {best} | Net profit: {profit}%")
                for ev in ev_list:
                    print(f"    {ev['source']}: {ev['odds']}")
                if is_new_arb(match_str + "_btts", history):
                    send_telegram_alert(match_str, profit)

        elif market_type == "dc":
            best = {"1X": 0, "12": 0, "X2": 0}
            for ev in ev_list:
                if "1X" in ev["odds"]:
                    best["1X"] = max(best["1X"], ev["odds"]["1X"])
                if "12" in ev["odds"]:
                    best["12"] = max(best["12"], ev["odds"]["12"])
                if "X2" in ev["odds"]:
                    best["X2"] = max(best["X2"], ev["odds"]["X2"])
            if all(best[k] > 0 for k in best):
                prob = sum(1/best[k] for k in best)
                if prob < 1:
                    raw_profit = (1 - prob) * 100
                    if raw_profit <= MAX_PROFIT:
                        profit = net_profit(raw_profit)
                        if profit > 0:
                            arbs_found += 1
                            print(f"\nARB FOUND (DC): {match_str}")
                            print(f"  Best odds: {best} | Net profit: {profit}%")
                            for ev in ev_list:
                                print(f"    {ev['source']}: {ev['odds']}")
                            if is_new_arb(match_str + "_dc", history):
                                send_telegram_alert(match_str, profit)

    save_history(history)

    if arbs_found == 0:
        print("\nNo profitable arbitrage opportunities found this scan.")
    else:
        print(f"\nTotal arbs found: {arbs_found}")

# ---------- Main Loop ----------
if __name__ == "__main__":
    print("Starting full arbitrage scanner. Press Ctrl+C to stop.")
    while True:
        try:
            scan()
            print(f"\nWaiting {POLL_INTERVAL} seconds...\n")
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\nScanner stopped by user.")
            break
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(10)
