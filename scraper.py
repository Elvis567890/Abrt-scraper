import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
import os
from copy import deepcopy

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SPORTYBET_API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"
CHAMPIONBET_API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
CHAMPIONBET_MATCH_API = "https://www.championbet.ug/restapi/offer/en/match/{match_id}?annex=13&mobileVersion=2.47.4.3&locale=en"

STAKE = 100000
HISTORY_FILE = "arb_history.json"

SHARED_BOOKMAKERS_1X = {
    "1xBet": {"base_url": "https://1xbet.ug", "partner": "135"},
    "22Bet": {"base_url": "https://22bet.ug", "partner": "151"},
}


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


def match_key_similarity(key1, key2):
    if "|" in key1 or "|" in key2:
        return key1 == key2
    parts1 = key1.split(" vs ")
    parts2 = key2.split(" vs ")
    if len(parts1) != 2 or len(parts2) != 2:
        return False
    return teams_match(parts1[0], parts2[0]) and teams_match(parts1[1], parts2[1])


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


def build_match_record(home_team, away_team, bookmaker, home, draw, away, sport="Football", competition=""):
    return {
        "match": f"{home_team} vs {away_team}",
        "home_team": home_team,
        "away_team": away_team,
        "match_key": f"{normalize(home_team)} vs {normalize(away_team)}",
        "bookmaker": bookmaker,
        "competition": competition,
        "home": home,
        "draw": draw,
        "away": away,
        "sport": sport,
    }


def normalize_sport_name(record):
    raw = (record.get("sport") or "").strip().lower()
    if not raw:
        record["sport"] = "Football"
        return record
    if "foot" in raw or "soccer" in raw:
        record["sport"] = "Football"
    elif "basket" in raw:
        record["sport"] = "Basketball"
    elif "netball" in raw:
        record["sport"] = "Netball"
    elif "tennis" in raw:
        record["sport"] = "Tennis"
    elif "rugby" in raw:
        record["sport"] = "Rugby"
    elif "futsal" in raw:
        record["sport"] = "Futsal"
    else:
        record["sport"] = raw.title()
    return record


def load_arbitrage_history():
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_arbitrage_history(arb_history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(arb_history, f, indent=2)


def opportunity_key(opp):
    return f"{opp['sport']}::{opp['type']}::{opp['match']}"


def update_arbitrage_history(current_opportunities, arb_history, timestamp_str):
    for history in arb_history.values():
        history["updated_this_cycle"] = False

    for opp in current_opportunities:
        key = opportunity_key(opp)
        if key not in arb_history:
            entry = {
                "match": opp["match"],
                "sport": opp["sport"],
                "type": opp["type"],
                "first_seen": timestamp_str,
                "last_seen": timestamp_str,
                "valid": True,
                "cycles_missed": 0,
                "versions": [],
            }
            arb_history[key] = entry

        entry = arb_history[key]
        entry["last_seen"] = timestamp_str
        entry["valid"] = True
        entry["cycles_missed"] = 0
        entry["updated_this_cycle"] = True

        version = {
            "timestamp": timestamp_str,
            "profit_percent": opp["profit_percent"],
            "profit_ugx": opp["profit_ugx"],
            "arb_sum": opp["arb_sum"],
            "bets": deepcopy(opp["bets"]),
        }
        entry["versions"].append(version)

    for key, entry in arb_history.items():
        if not entry.get("updated_this_cycle"):
            entry["cycles_missed"] = entry.get("cycles_missed", 0) + 1
            if entry["cycles_missed"] >= 2:
                entry["valid"] = False

    for entry in arb_history.values():
        if "updated_this_cycle" in entry:
            del entry["updated_this_cycle"]


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


def scrape_championbet():
    odds = []
    try:
        print("Fetching ChampionBet...")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Referer": "https://www.championbet.ug/mob/",
            "X-INSTANA-T": "2fbd167006ebd264",
            "X-INSTANA-S": "2fbd167006ebd264",
            "X-INSTANA-L": "1,correlationType=web;correlationId=2fbd167006ebd264",
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
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", h, d, a, competition=m.get("leagueName", "")))

                over, under = championbet_extract_ou_from_betmap(bet_map)
                if over and under:
                    record = build_match_record(home_team, away_team, "ChampionBet", over, under, None)
                    record["match_key"] = f"{normalize(home_team)} vs {normalize(away_team)} | O/U 2.5"
                    record["type"] = "Over/Under 2.5"
                    odds.append(record)

                time.sleep(0.2)
            except:
                continue
        print(f"ChampionBet: {count} matches extracted")
    except Exception as e:
        print(f"ChampionBet error: {e}")
    return odds


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
                    odds.append(build_match_record(home, away, "AbaBet", h, d, a, competition=row.get("League", "")))

                over = row.get("Over"); under = row.get("Under")
                if over and under:
                    record = build_match_record(home, away, "AbaBet", over, under, None)
                    record["match_key"] = f"{normalize(home)} vs {normalize(away)} | O/U 2.5"
                    record["type"] = "Over/Under 2.5"
                    odds.append(record)

        print(f"AbaBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"AbaBet error: {e}")
    return odds


def scrape_betpawa():
    print("Fetching BetPawa...") # <-- Added this print to track it in the logs
    odds = []
    seen_matches = set()
    urls = ["https://www.betpawa.ug/events?categoryId=2&marketId=1X2", "https://www.betpawa.ug/events/popular"]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            context = browser.new_context(user_agent="Mozilla/5.0", viewport={"width": 1920, "height": 1080}, locale="en-UG")
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            for url in urls:
                try:
                    page.goto(url, timeout=60000)
                    # Wait for events to load so Playwright doesn't pull empty lists
                    page.wait_for_selector('a[href*="/event/"], a[href*="/match/"]', timeout=30000)
                    links = page.query_selector_all('a[href*="/event/"], a[href*="/match/"]')
                    for link in links[:60]:
                        try:
                            text = link.inner_text()
                            parts = [p.strip() for p in text.split("\n") if p.strip()]
                            teams, odd_values, competition = [], [], ""
                            for part in parts:
                                if re.match(r"^\d+\.\d+$", part):
                                    odd_values.append(float(part))
                                elif any(s in part for s in ["Football", "Soccer", "Netball", "Tennis", "Basketball"]):
                                    competition = part
                                elif part in ["1", "X", "2", "1X", "X2", "12"]:
                                    continue
                                elif len(part) > 2 and part not in ["Over", "Under", "O/U", "Over 2.5", "Under 2.5", "O 2.5", "U 2.5"]:
                                    if "Over" in part or "Under" in part:
                                        continue
                                    teams.append(part)
                            if len(teams) >= 2:
                                ou_parts = [p for p in parts if "Over" in p or "Under" in p]
                                if len(ou_parts) >= 2:
                                    pass
                        except: continue
                except Exception as e:
                    print(f"BetPawa URL error: {e}")
            browser.close()
        print(f"BetPawa total: {len(odds)} matches extracted")
    except Exception as e:
        print(f"BetPawa error: {e}")
    return odds


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

                for market in event_markets.get(eid, []):
                    mid = market.get("marketId")
                    if mid == 1: # 1X2
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
                    elif mid == 5: # Total Goals
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1: over = clean_odd(v["odds"])
                                elif oid == 2: under = clean_odd(v["odds"])

                if h and a:
                    sport_name = "Netball" if d is None else "Football"
                    ev_sport = (event.get("sportName") or event.get("sport") or "").lower()
                    if "basketball" in ev_sport: sport_name = "Basketball"
                    elif "tennis" in ev_sport: sport_name = "Tennis"
                    count += 1
                    odds.append(build_match_record(home, away, "Fortebet", h, d, a, sport=sport_name))

                if over and under:
                    record = build_match_record(home, away, "Fortebet", over, under, None, sport="Football")
                    record["match_key"] = f"{normalize(home)} vs {normalize(away)} | O/U 2.5"
                    record["type"] = "Over/Under 2.5"
                    odds.append(record)
            except: continue
        print(f"Fortebet: {count} matches extracted")
    except Exception as e:
        print(f"Fortebet error: {e}")
    return odds


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
                        odds.append(build_match_record(home, away, "SportyBet", h, d, a, sport=sport))

                    over = clean_odd(event.get("over_odd"))
                    under = clean_odd(event.get("under_odd"))
                    if over and under:
                        record = build_match_record(home, away, "SportyBet", over, under, None, sport=sport)
                        record["match_key"] = f"{normalize(home)} vs {normalize(away)} | O/U 2.5"
                        record["type"] = "Over/Under 2.5"
                        odds.append(record)
                except: continue
        print(f"SportyBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"SportyBet error: {e}")
    return odds


def scrape_betika():
    odds = []
    try:
        print("Fetching Betika Uganda...")
        url = "https://api-ug.betika.com/v1/uo/matches?page=1&limit=200&tab=&sub_type_id=1,2&sport_id=3&sort_id=1&period_id=-1&esports=false"
        headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0", "Referer": "https://www.betika.com/en-ug/"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        matches = data.get("data", []) if isinstance(data, dict) else []
        count = 0
        for m in matches:
            try:
                home, away = m.get("home_team") or "", m.get("away_team") or ""
                if not home or not away: continue
                markets = m.get("odds") or m.get("sub_types") or []
                h = d = a = over = under = None

                for market in markets:
                    stid = str(market.get("sub_type_id"))
                    if stid == "1": # 1X2
                        for sel in market.get("odds", []):
                            outcome = (sel.get("odd_type") or sel.get("name") or "").strip()
                            price = clean_odd(sel.get("value") or sel.get("odd_value"))
                            if not price: continue
                            if outcome in ("1", "Home"): h = price
                            elif outcome in ("X", "Draw"): d = price
                            elif outcome in ("2", "Away"): a = price
                    elif stid == "2": # Total Goals
                        for sel in market.get("odds", []):
                            outcome = (sel.get("odd_type") or sel.get("name") or "").strip()
                            price = clean_odd(sel.get("value") or sel.get("odd_value"))
                            if not price: continue
                            if outcome in ("Over", "Over 2.5"): over = price
                            elif outcome in ("Under", "Under 2.5"): under = price

                if h and a:
                    count += 1
                    odds.append(build_match_record(home, away, "Betika", h, d, a, competition=m.get("competition_name", "")))
                if over and under:
                    record = build_match_record(home, away, "Betika", over, under, None, competition=m.get("competition_name", ""))
                    record["match_key"] = f"{normalize(home)} vs {normalize(away)} | O/U 2.5"
                    record["type"] = "Over/Under 2.5"
                    odds.append(record)
            except: continue
        print(f"Betika: {count} matches extracted")
    except Exception as e:
        print(f"Betika error: {e}")
    return odds


def scrape_1x_over_under(bookmaker_name, base_url, partner_id):
    odds = []
    try:
        print(f"Fetching {bookmaker_name} Over/Under...")
        url = f"{base_url}/service-api/LineFeed/Get1x2_VZip?count=1000&lng=en&mode=4&country=191&partner={partner_id}&getEmpty=true&virtualSports=true"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        for match in data.get("Value", []):
            home, away = match.get("O1"), match.get("O2")
            if not home or not away: continue
            over = under = None
            for e in match.get("E", []):
                t = str(e.get("T", "")).strip()
                c = clean_odd(e.get("C"))
                if not c: continue
                if t == "5": over = c
                elif t == "6": under = c
            if over and under:
                record = build_match_record(home, away, bookmaker_name, over, under, None)
                record["match_key"] = f"{normalize(home)} vs {normalize(away)} | O/U 2.5"
                record["type"] = "Over/Under 2.5"
                odds.append(record)
    except Exception as e:
        print(f"{bookmaker_name} Over/Under error: {e}")
    return odds


def scrape_1xbet():
    return scrape_1x_over_under("1xBet", SHARED_BOOKMAKERS_1X["1xBet"]["base_url"], SHARED_BOOKMAKERS_1X["1xBet"]["partner"])
def scrape_22bet():
    return scrape_1x_over_under("22Bet", SHARED_BOOKMAKERS_1X["22Bet"]["base_url"], SHARED_BOOKMAKERS_1X["22Bet"]["partner"])


def scrape_gsb():
    odds = []
    try:
        print("Fetching GSB...")
        base_url = "https://gsb.ug/services/evapi/event/GetEvents"
        params = {"timestamp": str(int(datetime.utcnow().timestamp() * 1000)), "betTypeIds": "-1", "sportTypeIds": "31", "statusId": "0"}
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        headers = {"Accept": "*/*", "BrandId": "112", "ChannelId": "4", "Language": "en-US", "Terminal": "gsb.ug", "User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        for ev in data.get("data", []):
            try:
                if ev.get("sid") != 31: continue
                home, away = ev.get("h") or "", ev.get("a") or ""
                if not home or not away: continue
                h = d = a = over = under = None
                for bt in ev.get("bts", []) or []:
                    name = (bt.get("n") or "").strip()
                    if name == "FT 1X2":
                        for o in bt.get("odds", []) or []:
                            sel = (o.get("n") or "").strip()
                            price = clean_odd(o.get("p"))
                            if not price: continue
                            if sel == "1": h = price
                            elif sel == "X": d = price
                            elif sel == "2": a = price
                    elif name == "Total Goals":
                        for o in bt.get("odds", []) or []:
                            sel = (o.get("n") or "").strip()
                            price = clean_odd(o.get("p"))
                            if not price: continue
                            if sel == "Over": over = price
                            elif sel == "Under": under = price
                if h and a:
                    odds.append(build_match_record(home, away, "GSB", h, d, a, sport=ev.get("sn", "Football"), competition=ev.get("ln", "")))
                if over and under:
                    record = build_match_record(home, away, "GSB", over, under, None, sport="Football", competition=ev.get("ln", ""))
                    record["match_key"] = f"{normalize(home)} vs {normalize(away)} | O/U 2.5"
                    record["type"] = "Over/Under 2.5"
                    odds.append(record)
            except: continue
    except Exception as e:
        print(f"GSB error: {e}")
    return odds


# ==========================================
# FIXED DNS ERRORS FOR BETWAY AND PREMIERBET
# ==========================================
def scrape_betway_ug():
    odds = []
    try:
        print("Fetching Betway Uganda...")
        url = "https://ug.betway.com/api/v1/SportsBook/GetEvents?SportId=1" # Corrected URL
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        for event in data.get("Events", []):
            home, away = event.get("HomeTeamName", ""), event.get("AwayTeamName", "")
            if not home or not away: continue
            h = d = a = over = under = None
            for market in event.get("Markets", []):
                market_name = market.get("Name", "")
                if market_name == "Match Winner":
                    for sel in market.get("Selections", []):
                        name = sel.get("Name", ""); price = clean_odd(sel.get("Price"))
                        if name == "1": h = price
                        elif name == "X": d = price
                        elif name == "2": a = price
                elif "Total" in market_name or "Over/Under" in market_name:
                    for sel in market.get("Selections", []):
                        name = sel.get("Name", ""); price = clean_odd(sel.get("Price"))
                        if name == "Over" or name == "Over 2.5": over = price
                        elif name == "Under" or name == "Under 2.5": under = price
            if h and a:
                odds.append(build_match_record(home, away, "Betway", h, d, a))
            if over and under:
                record = build_match_record(home, away, "Betway", over, under, None)
                record["match_key"] = f"{normalize(home)} vs {normalize(away)} | O/U 2.5"
                record["type"] = "Over/Under 2.5"
                odds.append(record)
    except Exception as e:
        print(f"Betway error: {e}")
    return odds


def scrape_premierbet_ug():
    odds = []
    try:
        print("Fetching PremierBet Uganda...")
        url = "https://premierbet.ug/api/v1/events?page=1&page_size=100&sport_id=1" # Corrected URL
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        for event in data.get("results", []):
            home, away = event.get("home_team", ""), event.get("away_team", "")
            if not home or not away: continue
            h = clean_odd(event.get("home_odd"))
            d = clean_odd(event.get("draw_odd"))
            a = clean_odd(event.get("away_odd"))
            if h and a:
                odds.append(build_match_record(home, away, "PremierBet", h, d, a))
            over = clean_odd(event.get("over_odd"))
            under = clean_odd(event.get("under_odd"))
            if over and under:
                record = build_match_record(home, away, "PremierBet", over, under, None)
                record["match_key"] = f"{normalize(home)} vs {normalize(away)} | O/U 2.5"
                record["type"] = "Over/Under 2.5"
                odds.append(record)
    except Exception as e:
        print(f"PremierBet error: {e}")
    return odds


def find_arbitrage(all_odds):
    opportunities = []
    sports_odds = {}
    for odd in all_odds:
        sport = odd.get("sport", "Football")
        sports_odds.setdefault(sport, []).append(odd)

    for sport, sport_odds in sports_odds.items():
        exact_groups = {}
        for odd in sport_odds:
            exact_groups.setdefault(odd.get("match_key", ""), []).append(odd)

        merged_groups, processed_keys = {}, set()
        all_keys = list(exact_groups.keys())
        for i, key1 in enumerate(all_keys):
            if key1 in processed_keys: continue
            group = list(exact_groups[key1])
            processed_keys.add(key1)
            for key2 in all_keys[i+1:]:
                if key2 in processed_keys: continue
                if match_key_similarity(key1, key2):
                    group.extend(exact_groups[key2])
                    processed_keys.add(key2)
            merged_groups[key1] = group

        for match_name, bookmakers in merged_groups.items():
            is_ou = "O/U 2.5" in match_name
            if len(set(b["bookmaker"] for b in bookmakers)) < 2: continue

            bk_odds = {}
            for b in bookmakers:
                bk = b["bookmaker"]
                bk_odds.setdefault(bk, {"home": 0.0, "draw": 0.0, "away": 0.0})
                home = clean_odd(b.get("home"))
                draw = clean_odd(b.get("draw"))
                away = clean_odd(b.get("away"))
                if home is not None and home > bk_odds[bk]["home"]: bk_odds[bk]["home"] = home
                if draw is not None and draw > bk_odds[bk]["draw"]: bk_odds[bk]["draw"] = draw
                if away is not None and away > bk_odds[bk]["away"]: bk_odds[bk]["away"] = away

            bk_list = list(bk_odds.keys())

            if is_ou:
                best = None
                for bk_over in bk_list:
                    for bk_under in bk_list:
                        if bk_over == bk_under: continue
                        over = bk_odds[bk_over]["home"]
                        under = bk_odds[bk_under]["draw"]
                        if not over or not under: continue
                        arb = (1 / over) + (1 / under)
                        if arb < 1:
                            profit = round((1 - arb) * 100, 2)
                            if 0.5 <= profit <= 20.0:
                                stake_over = round(STAKE * (1 / over) / arb)
                                stake_under = round(STAKE * (1 / under) / arb)
                                best = {
                                    "match": match_name.replace(" | O/U 2.5", ""),
                                    "sport": "Football", "type": "Over/Under 2.5",
                                    "profit_percent": profit, "profit_ugx": round(STAKE * (1 - arb)),
                                    "total_stake": STAKE, "arb_sum": round(arb, 4),
                                    "bets": [
                                        {"bookmaker": bk_over, "outcome": "Over 2.5", "odd": over, "stake": stake_over, "win": round(stake_over * over)},
                                        {"bookmaker": bk_under, "outcome": "Under 2.5", "odd": under, "stake": stake_under, "win": round(stake_under * under)}
                                    ]
                                }
                if best: opportunities.append(best)

            elif sport in ["Football", "Rugby", "Futsal"]:
                best = None
                for bk_h in bk_list:
                    for bk_d in bk_list:
                        for bk_a in bk_list:
                            if len({bk_h, bk_d, bk_a}) < 3: continue
                            h, d, a = bk_odds[bk_h]["home"], bk_odds[bk_d]["draw"], bk_odds[bk_a]["away"]
                            if not h or not d or not a: continue
                            arb = (1 / h) + (1 / d) + (1 / a)
                            if arb < 1:
                                profit = round((1 - arb) * 100, 2)
                                if 0.5 <= profit <= 20.0:
                                    stake_h = round(STAKE * (1 / h) / arb)
                                    stake_d = round(STAKE * (1 / d) / arb)
                                    stake_a = round(STAKE * (1 / a) / arb)
                                    best = {
                                        "match": match_name, "sport": sport, "type": "3-way",
                                        "profit_percent": profit, "profit_ugx": round(STAKE * (1 - arb)),
                                        "total_stake": STAKE, "arb_sum": round(arb, 4),
                                        "bets": [
                                            {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stake_h, "win": round(stake_h * h)},
                                            {"bookmaker": bk_d, "outcome": "Draw", "odd": d, "stake": stake_d, "win": round(stake_d * d)},
                                            {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stake_a, "win": round(stake_a * a)}
                                        ]
                                    }
                if best: opportunities.append(best)

            else:
                best = None
                for bk_h in bk_list:
                    for bk_a in bk_list:
                        if bk_h == bk_a: continue
                        h, a = bk_odds[bk_h]["home"], bk_odds[bk_a]["away"]
                        if not h or not a: continue
                        arb = (1 / h) + (1 / a)
                        if arb < 1:
                            profit = round((1 - arb) * 100, 2)
                            if 0.5 <= profit <= 20.0:
                                stake_h = round(STAKE * (1 / h) / arb)
                                stake_a = round(STAKE * (1 / a) / arb)
                                best = {
                                    "match": match_name, "sport": sport, "type": "2-way",
                                    "profit_percent": profit, "profit_ugx": round(STAKE * (1 - arb)),
                                    "total_stake": STAKE, "arb_sum": round(arb, 4),
                                    "bets": [
                                        {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stake_h, "win": round(stake_h * h)},
                                        {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stake_a, "win": round(stake_a * a)}
                                    ]
                                }
                if best: opportunities.append(best)

    return opportunities


def run_scan():
    all_odds = []
    all_odds.extend(scrape_sportybet())
    all_odds.extend(scrape_championbet())
    all_odds.extend(scrape_ababet())
    all_odds.extend(scrape_betpawa())
    all_odds.extend(scrape_fortebet())
    all_odds.extend(scrape_betika())
    all_odds.extend(scrape_1xbet())
    all_odds.extend(scrape_22bet())
    all_odds.extend(scrape_gsb())
    all_odds.extend(scrape_betway_ug())
    all_odds.extend(scrape_premierbet_ug())

    opportunities = find_arbitrage(all_odds)
    arb_history = load_arbitrage_history()
    timestamp_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    update_arbitrage_history(opportunities, arb_history, timestamp_str)
    save_arbitrage_history(arb_history)

    with open("current_opportunities.json", "w", encoding="utf-8") as f:
        json.dump(opportunities, f, indent=2)

    print(f"Scan complete: {len(opportunities)} opportunities, history + current_opportunities.json updated.")


if __name__ == "__main__":
    run_scan()
