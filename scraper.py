#!/usr/bin/env python3
# scraper.py – Full scanner (runs once per execution)

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
# GSB
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
        skip = 0
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
# TOPBET
# =========================

class TopBet(BaseBookmaker):
    name = "TopBet"

    CATEGORIES_API = "https://www.topbet.ug/restapi/offer/en/categories/sport/S/l?annex=13&mobileVersion=2.47.4.6&locale=en"
    LEAGUE_API = "https://www.topbet.ug/restapi/offer/en/sport/S/league/{league_id}/mob?annex=13&mobileVersion=2.47.4.6&locale=en"

    API_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0"
    }

    def get_leagues(self):
        r = requests.get(self.CATEGORIES_API, headers=self.API_HEADERS, timeout=TIMEOUT)
        data = r.json()
        leagues = []
        for cat in data.get("categories", []):
            if cat.get("type") == "LEAGUE" and cat.get("count", 0) > 0:
                leagues.append(cat["id"])
        return leagues

    def fetch_league(self, league_id):
        events = []
        try:
            r = requests.get(self.LEAGUE_API.format(league_id=league_id), headers=self.API_HEADERS, timeout=TIMEOUT)
            data = r.json()
            for match in data.get("esMatches", []):
                home = match.get("home")
                away = match.get("away")
                if not home or not away:
                    continue
                league = match.get("leagueName", "")
                bet_map = match.get("betMap", {})

                # 1x2 (keys "1","2","3" = home, draw, away)
                h = clean_odd(bet_map.get("1", {}).get("NULL", {}).get("ov"))
                d = clean_odd(bet_map.get("2", {}).get("NULL", {}).get("ov"))
                a = clean_odd(bet_map.get("3", {}).get("NULL", {}).get("ov"))
                if h and a:
                    events.append({
                        "bookmaker": self.name,
                        "league": league,
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": {"1": h, "X": d, "2": a}
                    })

                # Over/Under: key "227" = Over, "228" = Under, each has subkeys like "total=2.5"
                over_map = bet_map.get("227", {})
                under_map = bet_map.get("228", {})
                for line in ["total=1.5", "total=2.5"]:
                    if line in over_map and line in under_map:
                        over = clean_odd(over_map[line].get("ov"))
                        under = clean_odd(under_map[line].get("ov"))
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

                # Double Chance: keys "397" = 1X, "398" = 12, "399" = X2
                dc_map = bet_map.get("397", {})
                dc_1x = clean_odd(dc_map.get("NULL", {}).get("ov")) if "NULL" in dc_map else None
                dc_map = bet_map.get("398", {})
                dc_12 = clean_odd(dc_map.get("NULL", {}).get("ov")) if "NULL" in dc_map else None
                dc_map = bet_map.get("399", {})
                dc_x2 = clean_odd(dc_map.get("NULL", {}).get("ov")) if "NULL" in dc_map else None
                if dc_1x and dc_12 and dc_x2:
                    events.append({
                        "bookmaker": self.name,
                        "league": league,
                        "home": home,
                        "away": away,
                        "market": "dc",
                        "odds": {"1X": dc_1x, "12": dc_12, "X2": dc_x2}
                    })

        except Exception as e:
            pass
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
# 22BET
# =========================

class Bet22(BaseBookmaker):
    name = "22Bet"

    SPORT_INFO_API = "https://22bet.ug/service-api/RestCore/api/External/v1/Web/SportInfo?lng=en_GB&ref=151&gr=525&fcountry=191"
    EVENTS_API = "https://22bet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en_GB&tz=3&country=191&partner=151&gr=525&getEmpty=true&virtualSports=true"

    API_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0"
    }

    def fetch(self):
        events = []
        try:
            r = requests.get(self.EVENTS_API, headers=self.API_HEADERS, timeout=TIMEOUT)
            data = r.json()
            for match in data.get("Value", []):
                home = match.get("O1")
                away = match.get("O2")
                if not home or not away:
                    continue
                league = match.get("L", "")
                odds_map = {}
                for e in match.get("E", []):
                    t = e.get("T")
                    c = clean_odd(e.get("C"))
                    if c:
                        odds_map[(t, e.get("P"))] = c

                h = odds_map.get((1, None))
                d = odds_map.get((2, None))
                a = odds_map.get((3, None))
                if h and a:
                    events.append({
                        "bookmaker": self.name,
                        "league": league,
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": {"1": h, "X": d, "2": a}
                    })
        except Exception as e:
            print(f"{self.name} error: {e}")
        return events

# =========================
# BETMASTER
# =========================

class Betmaster(BaseBookmaker):
    name = "Betmaster"

    API = "https://betmasterug.com/Sports.aspx/GetSportMarkets"

    def fetch(self):
        payload = {
            "sportid": "1",
            "countryid": "",
            "leagueid": "",
            "isfeatured": 0,
            "searchteam": 0,
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
# CHAMPIONBET
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
# ABABET
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
# FORTEBET
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
# SPORTYBET (WORKS)
# =========================

class SportyBet(BaseBookmaker):
    name = "SportyBet"

    API = "https://www.sportybet.com/factsCenter/wapConfigurableEventsByOrder"

    API_HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    def fetch(self):
        events = []
        try:
            payload = {
                "productId": 3,
                "sportId": "sr:sport:1",
                "order": 0,
                "pageNum": 1,
                "pageSize": 20,
                "withTwoUpMarket": True,
                "withOneUpMarket": True
            }
            r = requests.post(self.API, headers=self.API_HEADERS, json=payload, timeout=TIMEOUT)
            data = r.json()
            tournaments = data.get("data", {}).get("tournaments", [])
            for tourn in tournaments:
                for ev in tourn.get("events", []):
                    home = ev.get("homeTeamName")
                    away = ev.get("awayTeamName")
                    if not home or not away:
                        continue
                    league = tourn.get("name", "")

                    # Extract 1x2
                    h = d = a = None
                    # Over/Under 1.5
                    over = under = None

                    for market in ev.get("markets", []):
                        mid = market.get("id")
                        if mid == "1":  # 1x2
                            for outcome in market.get("outcomes", []):
                                oid = outcome.get("id")
                                odd = clean_odd(outcome.get("odds"))
                                if oid == "1" and odd:
                                    h = odd
                                elif oid == "2" and odd:
                                    d = odd
                                elif oid == "3" and odd:
                                    a = odd
                        elif mid == "18":  # Over/Under
                            specifier = market.get("specifier", "")
                            if specifier == "total=1.5":
                                for outcome in market.get("outcomes", []):
                                    oid = outcome.get("id")
                                    odd = clean_odd(outcome.get("odds"))
                                    if oid == "12" and odd:  # Over
                                        over = odd
                                    elif oid == "13" and odd:  # Under
                                        under = odd

                    if h and a:
                        events.append({
                            "bookmaker": self.name,
                            "league": league,
                            "home": home,
                            "away": away,
                            "market": "1x2",
                            "odds": {"1": h, "X": d, "2": a}
                        })
                    if over and under:
                        events.append({
                            "bookmaker": self.name,
                            "league": league,
                            "home": home,
                            "away": away,
                            "market": "over15",
                            "odds": {"over": over, "under": under}
                        })
        except Exception as e:
            print(f"{self.name} error: {e}")
        return events

# =========================
# MELBET
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
# 1XBET
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
# PARSEBOT
# =========================

class ParseBot(BaseBookmaker):
    name = "ParseBot"

    API = "https://api.parse.bot/scraper/8ffd9f0c-6174-43af-80dc-4898f47f074b/get_upcoming_events"

    def fetch(self):
        events = []
        try:
            params = {
                "page": 1,
                "sport": "football",
                "page_size": 50,
                "market_ids": "1,18"
            }
            r = requests.get(self.API, params=params, headers=HEADERS, timeout=TIMEOUT)
            data = r.json()

            if isinstance(data, list):
                events_list = data
            elif isinstance(data, dict) and "data" in data:
                events_list = data["data"]
            else:
                events_list = []

            for ev in events_list:
                home = ev.get("home_team") or ev.get("home") or ev.get("homeTeam")
                away = ev.get("away_team") or ev.get("away") or ev.get("awayTeam")
                league = ev.get("league") or ev.get("competition") or ""
                if not home or not away:
                    continue

                odds = {}
                for market in ev.get("markets", []):
                    market_id = market.get("id")
                    if market_id == 1:
                        for outcome in market.get("outcomes", []):
                            name = outcome.get("name")
                            odd = clean_odd(outcome.get("odds"))
                            if name == "1" and odd:
                                odds["1"] = odd
                            elif name == "X" and odd:
                                odds["X"] = odd
                            elif name == "2" and odd:
                                odds["2"] = odd
                    elif market_id == 18:
                        for outcome in market.get("outcomes", []):
                            name = outcome.get("name")
                            line = outcome.get("line")
                            odd = clean_odd(outcome.get("odds"))
                            if name == "Over" and line == "1.5" and odd:
                                odds["over"] = odd
                            elif name == "Under" and line == "1.5" and odd:
                                odds["under"] = odd

                if "1" in odds and "X" in odds and "2" in odds:
                    events.append({
                        "bookmaker": self.name,
                        "league": league,
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": {"1": odds["1"], "X": odds["X"], "2": odds["2"]}
                    })
                if "over" in odds and "under" in odds:
                    events.append({
                        "bookmaker": self.name,
                        "league": league,
                        "home": home,
                        "away": away,
                        "market": "over15",
                        "odds": {"over": odds["over"], "under": odds["under"]}
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
        return []

# =========================
# BETPAWA (PLACEHOLDER)
# =========================

class BetPawa(BaseBookmaker):
    name = "BetPawa"
    def fetch(self):
        return []

# =========================
# ARBITRAGE ENGINE
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
# HTML DASHBOARD GENERATOR
# =========================

def generate_html_dashboard(arbs):
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Arbitrage Scanner – Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f4f4f4; margin: 20px; }
        h1 { color: #333; }
        .arb { background: #fff; border: 1px solid #ddd; border-radius: 5px; padding: 10px; margin-bottom: 10px; }
        .profit { color: green; font-weight: bold; }
        .market { color: #555; }
        .odds { margin-top: 5px; }
    </style>
</head>
<body>
    <h1>Arbitrage Opportunities – Last Scan</h1>
    <p>Total arbs: {total}</p>
    {arbs}
</body>
</html>"""

    arb_html = ""
    for arb in arbs:
        odds_text = ""
        for outcome, data in arb["best_odds"].items():
            odd, bookmaker = data if isinstance(data, tuple) else (data, "?")
            odds_text += f"{outcome}: {odd} @ {bookmaker}<br>"
        arb_html += f"""
        <div class="arb">
            <strong>{arb['match']}</strong> <span class="market">[{arb['market']}]</span><br>
            League: {arb['league']}<br>
            <span class="profit">Profit: {arb['profit']}%</span><br>
            <div class="odds">
                {odds_text}
            </div>
        </div>
        """

    with open("index.html", "w") as f:
        f.write(html.format(total=len(arbs), arbs=arb_html))

# =========================
# MAIN SCAN (RUNS ONCE)
# =========================

BOOKMAKERS = [
    GSB(),
    TopBet(),
    Bet22(),
    Betmaster(),
    ChampionBet(),
    AbaBet(),
    Fortebet(),
    SportyBet(),
    Melbet(),
    OneXBet(),
    ParseBot(),
    BongoBongo(),
    BetPawa(),
]

def scan_once():
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

    # Save all data
    with open("events.json", "w") as f:
        json.dump(all_events, f, indent=2)
    with open("current_opportunities.json", "w") as f:
        json.dump(arbs, f, indent=2)
    generate_html_dashboard(arbs)
    print("\nSaved events.json, current_opportunities.json, index.html")
    print("SCAN COMPLETE – Exiting.")

# =========================
# RUN ONCE
# =========================

if __name__ == "__main__":
    scan_once()
