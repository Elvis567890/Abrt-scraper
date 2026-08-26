#!/usr/bin/env python3

import os
import re
import json
import time
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from bs4 import BeautifulSoup
import urllib.request

# =========================
# CONFIG
# =========================

POLL_INTERVAL = 15
TAX_RATE = 0.15
MAX_PROFIT = 60.0
TIMEOUT = 20
MAX_THREADS = 20

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

# =========================
# HELPERS
# =========================

def normalize(name):
    if not name:
        return ""
    name = name.lower().strip()
    replacements = {
        "united": "utd",
        "rovers": "rvs",
        "football club": "",
        "fc": "",
        "sc": "",
        "cf": "",
        "club": "",
    }
    for k, v in replacements.items():
        name = name.replace(k, v)
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()

def clean_odd(value):
    try:
        value = float(value)
        if 1.01 <= value <= 100.0:
            return value
    except:
        pass
    return None

def event_key(event):
    return (
        normalize(event["home"]),
        normalize(event["away"]),
        event["market"]
    )

def net_profit(raw_profit):
    return round(raw_profit * (1 - TAX_RATE), 2)

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )
    except:
        pass

# =========================
# BASE FETCHER
# =========================

class BaseBookmaker:
    name = "base"
    def fetch(self):
        return []

    def safe_get(self, url, **kwargs):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
        r.raise_for_status()
        return r.json()

# =========================
# GSB (YOUR ORIGINAL CLASS – FIXED)
# =========================

class GSB(BaseBookmaker):
    name = "GSB"

    TREE_API = "https://gsb.ug/services/evapi/event/GetSportsTree?statusId=0&eventTypeId=0"
    EVENTS_API = "https://gsb.ug/services/evapi/event/GetEvents"

    API_HEADERS = {
        "Accept": "*/*, application/json",
        "Content-Type": "application/json",
        "BrandId": "112",
        "ChannelId": "4",
        "Language": "en-US",
        "Terminal": "gsb.ug"
    }

    def get_leagues(self):
        r = requests.get(self.TREE_API, headers=self.API_HEADERS, timeout=TIMEOUT)
        data = r.json()
        leagues = []
        root = data["data"]
        soccer = next((x for x in root["cl"] if x["id"] == "31"), None)
        if not soccer:
            return []
        for country in soccer["cl"]:
            for league in country.get("cl", []):
                leagues.append(league["id"])
        return leagues

    def fetch_league(self, league_id):
        events = []
        skip = 0  # FIXED
        take = 100

        while True:
            params = {
                "betTypeIds": "-1",
                "take": take,
                "statusId": "0",
                "eventTypeId": "0",
                "leagueIds": league_id,
                "skip": skip,
                "sportTypeIds": "31"
            }
            try:
                r = requests.get(self.EVENTS_API, headers=self.API_HEADERS, params=params, timeout=TIMEOUT)
                data = r.json().get("data", [])
                if not data:
                    break
                for ev in data:
                    home = ev.get("h")
                    away = ev.get("a")
                    league = ev.get("ln")

                    # 1x2
                    market = next((x for x in ev.get("bts", []) if x.get("n") == "FT 1X2"), None)
                    if market:
                        odds = {}
                        for odd in market.get("odds", []):
                            if odd["n"] == "1":
                                odds["1"] = clean_odd(odd["p"])
                            elif odd["n"] == "X":
                                odds["X"] = clean_odd(odd["p"])
                            elif odd["n"] == "2":
                                odds["2"] = clean_odd(odd["p"])
                        if len(odds) == 3:
                            events.append({
                                "bookmaker": self.name,
                                "league": league,
                                "home": home,
                                "away": away,
                                "market": "1x2",
                                "odds": odds
                            })

                    # Over 1.5
                    market = next((x for x in ev.get("bts", []) if x.get("n") == "Under/Over"), None)
                    if market:
                        for odd in market.get("odds", []):
                            if odd.get("l") == "1.5":
                                over = clean_odd(odd.get("p")) if odd.get("n") == "over" else None
                                under = clean_odd(odd.get("p")) if odd.get("n") == "under" else None
                                if over and under:
                                    events.append({
                                        "bookmaker": self.name,
                                        "league": league,
                                        "home": home,
                                        "away": away,
                                        "market": "over15",
                                        "odds": {"over": over, "under": under}
                                    })
                                break

                    # BTTS
                    market = next((x for x in ev.get("bts", []) if x.get("n") == "GG/NG"), None)
                    if market:
                        yes = None
                        no = None
                        for odd in market.get("odds", []):
                            if odd.get("n") == "Yes":
                                yes = clean_odd(odd.get("p"))
                            elif odd.get("n") == "No":
                                no = clean_odd(odd.get("p"))
                        if yes and no:
                            events.append({
                                "bookmaker": self.name,
                                "league": league,
                                "home": home,
                                "away": away,
                                "market": "btts",
                                "odds": {"yes": yes, "no": no}
                            })

                    # Double Chance
                    market = next((x for x in ev.get("bts", []) if x.get("n") == "Double Chance"), None)
                    if market:
                        odds = {}
                        for odd in market.get("odds", []):
                            if odd.get("n") in ("1X", "12", "X2"):
                                odds[odd.get("n")] = clean_odd(odd.get("p"))
                        if len(odds) == 3:
                            events.append({
                                "bookmaker": self.name,
                                "league": league,
                                "home": home,
                                "away": away,
                                "market": "dc",
                                "odds": odds
                            })

                if len(data) < take:
                    break
                skip += take
            except:
                break
        return events

    def fetch(self):
        leagues = self.get_leagues()
        all_events = []
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = [executor.submit(self.fetch_league, lid) for lid in leagues]
            for future in as_completed(futures):
                try:
                    all_events.extend(future.result())
                except:
                    pass
        return all_events

# =========================
# BETMASTER (YOUR ORIGINAL CLASS – FIXED)
# =========================

class Betmaster(BaseBookmaker):
    name = "Betmaster"

    API = "https://betmasterug.com/Sports.aspx/GetSportMarkets"

    def fetch(self):
        payload = {
            "sportid": "1",
            "countryid": "",
            "leagueid": "",
            "isfeatured": 0,  # FIXED
            "searchteam": 0,  # FIXED
            "filter": 100
        }
        r = requests.post(self.API, headers=HEADERS, json=payload, timeout=TIMEOUT)
        data = json.loads(r.json()["d"])
        events = []
        for m in data:
            try:
                home = m["hometeam"]
                away = m["awayteam"]
                h = clean_odd(m.get("outcomeodd1market1"))
                d = clean_odd(m.get("outcomeodd2market1"))
                a = clean_odd(m.get("outcomeodd3market1"))
                if h and d and a:
                    events.append({
                        "bookmaker": self.name,
                        "league": m.get("LeagueName", ""),
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": {"1": h, "X": d, "2": a}
                    })
            except:
                continue
        return events

# =========================
# CHAMPIONBET (ADDED)
# =========================

class ChampionBet(BaseBookmaker):
    name = "ChampionBet"

    API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
    MATCH_API = "https://www.championbet.ug/restapi/offer/en/match/{match_id}?annex=13&mobileVersion=2.47.4.3&locale=en"

    def extract_1x2(self, bet_map):
        bet_map = bet_map or {}
        def pick_odd(keys):
            for k in keys:
                market = bet_map.get(str(k), {}) or {}
                if not isinstance(market, dict): continue
                for _, item in market.items():
                    if isinstance(item, dict):
                        odd = clean_odd(item.get("ov"))
                        if odd: return odd
            return None
        return pick_odd([1,4,7]), pick_odd([2,5,8]), pick_odd([3,6,9])

    def fetch(self):
        events = []
        try:
            r = requests.get(self.API, headers=HEADERS, timeout=TIMEOUT)
            top_data = r.json()
            matches = top_data.get("esMatches", []) if isinstance(top_data, dict) else []
            for m in matches:
                if "Soccer" not in str(m.get("sportToken", "")): continue
                match_id = m.get("id")
                if not match_id: continue
                home = m.get("home")
                away = m.get("away")
                if not home or not away: continue
                try:
                    mr = requests.get(self.MATCH_API.format(match_id=match_id), headers=HEADERS, timeout=TIMEOUT)
                    match_data = mr.json()
                    bet_map = match_data.get("betMap", {}) if isinstance(match_data, dict) else {}
                    h, d, a = self.extract_1x2(bet_map)
                    if h and a:
                        events.append({
                            "bookmaker": self.name,
                            "league": m.get("leagueName", ""),
                            "home": home,
                            "away": away,
                            "market": "1x2",
                            "odds": {"1": h, "X": d, "2": a}
                        })
                except:
                    continue
        except Exception as e:
            print(f"{self.name} error: {e}")
        return events

# =========================
# ABABET (ADDED)
# =========================

class AbaBet(BaseBookmaker):
    name = "AbaBet"

    URL = "https://www.ababet.ug/soccer/match_result?mobile=1"

    def fetch(self):
        events = []
        try:
            r = requests.get(self.URL, headers=HEADERS, timeout=TIMEOUT)
            soup = BeautifulSoup(r.text, "html.parser")
            tables = soup.find_all("table")
            for table in tables:
                first_row = table.find("tr")
                if not first_row: continue
                headers = [c.get_text(" ", strip=True) for c in first_row.find_all(["th","td"])]
                if "Home" not in headers or "Away" not in headers: continue
                for tr in table.find_all("tr")[1:]:
                    cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td","th"])]
                    if len(cells) < 5: continue
                    row = dict(zip(headers, cells[:len(headers)]))
                    home = row.get("Home")
                    away = row.get("Away")
                    if not home or not away or home == "-" or away == "-": continue
                    h = clean_odd(row.get("1"))
                    d = clean_odd(row.get("X"))
                    a = clean_odd(row.get("2"))
                    if h and a:
                        events.append({
                            "bookmaker": self.name,
                            "league": row.get("League", ""),
                            "home": home,
                            "away": away,
                            "market": "1x2",
                            "odds": {"1": h, "X": d, "2": a}
                        })
        except Exception as e:
            print(f"{self.name} error: {e}")
        return events

# =========================
# FORTEBET (ADDED)
# =========================

class Fortebet(BaseBookmaker):
    name = "Fortebet"

    API = "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en"

    def fetch(self):
        events = []
        try:
            r = requests.get(self.API, headers=HEADERS, timeout=TIMEOUT)
            data = r.json()
            inner = data.get("data", {})
            events_dict = inner.get("event", {})
            markets_dict = inner.get("markets", {})
            competitors = inner.get("competitors", {})
            event_markets = defaultdict(list)
            for _, market in markets_dict.items():
                event_markets[str(market.get("eventId", ""))].append(market)

            for eid, event in events_dict.items():
                comps = event.get("competitors", [])
                if len(comps) < 2: continue
                home = competitors.get(str(comps[0]), {}).get("name", "")
                away = competitors.get(str(comps[1]), {}).get("name", "")
                if not home or not away: continue

                h = d = a = None
                for market in event_markets.get(eid, []):
                    if market.get("marketId") == 1:
                        odd_list = []
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                odd_list.append((v.get("outcomeId", 0), clean_odd(v["odds"])))
                        odd_list = [(i, o) for i, o in odd_list if o]
                        odd_list.sort(key=lambda x: x[0])
                        if len(odd_list) >= 3:
                            h, d, a = odd_list[0][1], odd_list[1][1], odd_list[2][1]
                        elif len(odd_list) == 2:
                            h, a = odd_list[0][1], odd_list[1][1]
                        break

                if h and a:
                    events.append({
                        "bookmaker": self.name,
                        "league": event.get("tournamentName", ""),
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": {"1": h, "X": d, "2": a}
                    })
        except Exception as e:
            print(f"{self.name} error: {e}")
        return events

# =========================
# SPORTYBET (ADDED)
# =========================

class SportyBet(BaseBookmaker):
    name = "SportyBet"

    API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"

    def fetch(self):
        events = []
        try:
            r = requests.get(self.API, headers=HEADERS, timeout=TIMEOUT)
            data = r.json()
            if isinstance(data, list):
                for event in data:
                    home = event.get("home_team", "")
                    away = event.get("away_team", "")
                    h = clean_odd(event.get("home"))
                    d = clean_odd(event.get("draw"))
                    a = clean_odd(event.get("away"))
                    if h and a:
                        events.append({
                            "bookmaker": self.name,
                            "league": event.get("competition", ""),
                            "home": home,
                            "away": away,
                            "market": "1x2",
                            "odds": {"1": h, "X": d, "2": a}
                        })
        except Exception as e:
            print(f"{self.name} error: {e}")
        return events

# =========================
# MELBET (ADDED)
# =========================

class Melbet(BaseBookmaker):
    name = "Melbet"

    API = "https://melbet-424658.top/service-api/LineFeed/GetTopGamesStatZip"

    def fetch(self):
        events = []
        try:
            params = {"lng": "en", "antisports": "66", "partner": "8"}
            r = requests.get(self.API, headers=HEADERS, params=params, timeout=TIMEOUT)
            data = r.json()
            for event in data.get("Value", []):
                home = event.get("O1", "")
                away = event.get("O2", "")
                if not home or not away or event.get("SI") != 1:
                    continue
                odds_map = {}
                for item in event.get("E", []):
                    t = item.get("T")
                    c = clean_odd(item.get("C"))
                    if c:
                        odds_map[(t, item.get("P"))] = c
                h = odds_map.get((1, None))
                d = odds_map.get((2, None))
                a = odds_map.get((3, None))
                if h and a:
                    events.append({
                        "bookmaker": self.name,
                        "league": event.get("L", ""),
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": {"1": h, "X": d, "2": a}
                    })
        except Exception as e:
            print(f"{self.name} error: {e}")
        return events

# =========================
# 1XBET (ADDED)
# =========================

class OneXBet(BaseBookmaker):
    name = "1xBet"

    API = "https://1x-bet.mobi/service-api/main-live-feed/v3/games1x2"

    def fetch(self):
        events = []
        try:
            params = {"cfView": "3", "count": "50", "fcountry": "191", "gr": "455", "grMode": "4", "lng": "en", "ref": "1"}
            r = requests.get(self.API, headers=HEADERS, params=params, timeout=TIMEOUT)
            data = r.json()
            if not isinstance(data, list):
                return []
            for event in data:
                if event.get("sport", {}).get("id") != 1:
                    continue
                home = event.get("opponent1", {}).get("fullName", "")
                away = event.get("opponent2", {}).get("fullName", "")
                if not home or not away:
                    continue
                odds_map = {}
                for group in event.get("eventGroups", []):
                    gid = group.get("groupId")
                    for event_list in group.get("events", []):
                        for item in event_list:
                            t = item.get("type")
                            c = clean_odd(item.get("cf"))
                            if c:
                                odds_map[(gid, t, item.get("parameter"))] = c
                h = odds_map.get((1, 1, None))
                d = odds_map.get((1, 2, None))
                a = odds_map.get((1, 3, None))
                if h and a:
                    events.append({
                        "bookmaker": self.name,
                        "league": event.get("liga", {}).get("name", ""),
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": {"1": h, "X": d, "2": a}
                    })
        except Exception as e:
            print(f"{self.name} error: {e}")
        return events

# =========================
# BONGOBONGO (PLACEHOLDER)
# =========================

class BongoBongo(BaseBookmaker):
    name = "BongoBongo"
    def fetch(self):
        # TODO: Implement when real endpoint provided
        return []

# =========================
# BETPAWA (PLACEHOLDER)
# =========================

class BetPawa(BaseBookmaker):
    name = "BetPawa"
    def fetch(self):
        # TODO: Implement when protobuf schema available
        return []

# =========================
# ARBITRAGE ENGINE (YOUR LOGIC – FIXED)
# =========================

def find_arbs(events):
    grouped = defaultdict(list)
    for ev in events:
        grouped[event_key(ev)].append(ev)

    arbs = []

    for key, group in grouped.items():
        if len(group) < 2:
            continue
        market = key[2]

        if market == "1x2":
            best = {"1": (0, ""), "X": (0, ""), "2": (0, "")}
            for ev in group:
                for outcome, odd in ev["odds"].items():
                    if odd > best[outcome][0]:
                        best[outcome] = (odd, ev["bookmaker"])

            try:
                prob = 1 / best["1"][0] + 1 / best["X"][0] + 1 / best["2"][0]
            except ZeroDivisionError:
                continue

            if prob < 1:
                raw_profit = (1 - prob) * 100
                if raw_profit > MAX_PROFIT:
                    continue
                profit = net_profit(raw_profit)
                if profit <= 0:
                    continue

                arbs.append({
                    "match": f"{group[0]['home']} vs {group[0]['away']}",
                    "league": group[0]["league"],
                    "market": "1x2",
                    "profit": profit,
                    "best_odds": best
                })

        elif market == "over15":
            best_over = max(ev["odds"]["over"] for ev in group)
            best_under = max(ev["odds"]["under"] for ev in group)
            prob = 1 / best_over + 1 / best_under
            if prob < 1:
                raw_profit = (1 - prob) * 100
                if raw_profit > MAX_PROFIT:
                    continue
                profit = net_profit(raw_profit)
                if profit > 0:
                    arbs.append({
                        "match": f"{group[0]['home']} vs {group[0]['away']}",
                        "league": group[0]["league"],
                        "market": "Over 1.5",
                        "profit": profit,
                        "best_odds": {"over": best_over, "under": best_under}
                    })

        elif market == "btts":
            best_yes = max(ev["odds"]["yes"] for ev in group)
            best_no = max(ev["odds"]["no"] for ev in group)
            prob = 1 / best_yes + 1 / best_no
            if prob < 1:
                raw_profit = (1 - prob) * 100
                if raw_profit > MAX_PROFIT:
                    continue
                profit = net_profit(raw_profit)
                if profit > 0:
                    arbs.append({
                        "match": f"{group[0]['home']} vs {group[0]['away']}",
                        "league": group[0]["league"],
                        "market": "BTTS",
                        "profit": profit,
                        "best_odds": {"yes": best_yes, "no": best_no}
                    })

        elif market == "dc":
            best_1x = max(ev["odds"]["1X"] for ev in group)
            best_12 = max(ev["odds"]["12"] for ev in group)
            best_x2 = max(ev["odds"]["X2"] for ev in group)
            prob = 1 / best_1x + 1 / best_12 + 1 / best_x2
            if prob < 1:
                raw_profit = (1 - prob) * 100
                if raw_profit > MAX_PROFIT:
                    continue
                profit = net_profit(raw_profit)
                if profit > 0:
                    arbs.append({
                        "match": f"{group[0]['home']} vs {group[0]['away']}",
                        "league": group[0]["league"],
                        "market": "Double Chance",
                        "profit": profit,
                        "best_odds": {"1X": best_1x, "12": best_12, "X2": best_x2}
                    })

    return arbs

# =========================
# MAIN SCAN
# =========================

BOOKMAKERS = [
    GSB(),
    Betmaster(),
    ChampionBet(),
    AbaBet(),
    Fortebet(),
    SportyBet(),
    Melbet(),
    OneXBet(),
    BongoBongo(),
    BetPawa(),
]

def scan():
    all_events = []
    print("\n========================")
    print("STARTING SCAN")
    print("========================\n")

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(book.fetch): book.name
            for book in BOOKMAKERS
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                data = future.result()
                print(f"{name}: {len(data)} events")
                all_events.extend(data)
            except Exception as e:
                print(f"{name} failed: {e}")

    print(f"\nTOTAL EVENTS: {len(all_events)}")
    arbs = find_arbs(all_events)
    print(f"ARBS FOUND: {len(arbs)}\n")

    for arb in arbs:
        print("=" * 60)
        print(f"{arb['match']} [{arb['market']}]")
        print(arb["league"])
        print(f"PROFIT: {arb['profit']}%")
        for outcome, data in arb["best_odds"].items():
            odd, bookmaker = data if isinstance(data, tuple) else (data, "?")
            print(f"{outcome}: {odd} @ {bookmaker}")
        msg = (
            f"ARB FOUND\n\n"
            f"{arb['match']} [{arb['market']}]\n"
            f"{arb['league']}\n"
            f"Profit: {arb['profit']}%"
        )
        send_telegram(msg)

    with open("events.json", "w") as f:
        json.dump(all_events, f, indent=2)
    with open("arbs.json", "w") as f:
        json.dump(arbs, f, indent=2)

# =========================
# LOOP
# =========================

if __name__ == "__main__":
    while True:
        try:
            scan()
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("Fatal Error:", e)
            time.sleep(10)
