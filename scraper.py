import json
import re
import time
import urllib.parse          # <-- added for GSB scraper
import urllib.request
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SPORTYBET_API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"
CHAMPIONBET_API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
CHAMPIONBET_MATCH_API = "https://www.championbet.ug/restapi/offer/en/match/{match_id}?annex=13&mobileVersion=2.47.4.3&locale=en"

STAKE = 100000

# Shared 1xBet-style backend family configuration
SHARED_BOOKMAKERS_1X = {
    "1xBet": {
        "base_url": "https://1xbet.ug",
        "partner": "135",
    },
    "22Bet": {
        "base_url": "https://22bet.ug",
        "partner": "151",
    },
    "Melbet": {
        "base_url": "https://melbet.ug",
        "partner": "8",
    },
    # Add additional clones here (e.g. BetWinner) once confirmed
}


def normalize(name):
    name = (name or "").lower().strip()
    name = re.sub(r"\b(fc|sc|cf|ac|united|city|sports|club|utd|football|soccer|women|men|u21|u23)\b", "", name)
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


# NEW: small helper to normalize sport names across scrapers
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


# NEW: use detailed match betMap
def championbet_extract_1x2_from_betmap(bet_map):
    """
    Given a betMap dict from the detailed match endpoint, return (home, draw, away) odds.
    Uses your existing assumption: tt 1/4/7 = Home, 2/5/8 = Draw, 3/6/9 = Away.
    """
    bet_map = bet_map or {}

    def pick_odd(market_keys):
        for k in market_keys:
            market = bet_map.get(str(k), {}) or {}
            if not isinstance(market, dict):
                continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None:
                        return odd
        return None

    home = pick_odd([1, 4, 7])
    draw = pick_odd([2, 5, 8])
    away = pick_odd([3, 6, 9])
    return home, draw, away


def scrape_championbet():
    odds = []
    try:
        print("Fetching ChampionBet top list...")
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
                if "Soccer" not in sport_token:
                    continue

                match_id = m.get("id")
                if not match_id:
                    continue

                home_team = m.get("home") or ""
                away_team = m.get("away") or ""
                if not home_team or not away_team:
                    continue

                match_url = CHAMPIONBET_MATCH_API.format(match_id=match_id)
                match_req = urllib.request.Request(match_url, headers=headers)
                with urllib.request.urlopen(match_req, timeout=30) as r2:
                    match_data = json.loads(r2.read().decode())

                bet_map = match_data.get("betMap", {}) if isinstance(match_data, dict) else {}
                home_odd, draw_odd, away_odd = championbet_extract_1x2_from_betmap(bet_map)

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(
                        build_match_record(
                            home_team=home_team,
                            away_team=away_team,
                            bookmaker="ChampionBet",
                            home=home_odd,
                            draw=draw_odd,
                            away=away_odd,
                            sport="Football",
                            competition=m.get("leagueName", ""),
                        )
                    )

                time.sleep(0.2)

            except Exception as e:
                print(f"ChampionBet match error: {e}")
                continue

        print(f"ChampionBet: {count} matches extracted with detailed betMap")
    except Exception as e:
        print(f"ChampionBet error: {e}")
    return odds


def scrape_ababet():
    odds = []
    try:
        print("Fetching AbaBet...")
        url = "https://www.ababet.ug/soccer/match_result?mobile=1"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            print("AbaBet: no tables found")
            return odds

        for table in tables:
            first_row = table.find("tr")
            if not first_row:
                continue

            table_headers = [c.get_text(" ", strip=True) for c in first_row.find_all(["th", "td"])]
            if not table_headers or "Home" not in table_headers or "Away" not in table_headers:
                continue

            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 5:
                    continue

                row = dict(zip(table_headers, cells[:len(table_headers)]))
                home_team = row.get("Home")
                away_team = row.get("Away")
                if not home_team or not away_team or home_team == "-" or away_team == "-":
                    continue

                odds.append(build_match_record(
                    home_team=home_team,
                    away_team=away_team,
                    bookmaker="AbaBet",
                    home=row.get("1"),
                    draw=row.get("X"),
                    away=row.get("2"),
                    sport="Football",
                    competition=row.get("League", "")
                ))

        print(f"AbaBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"AbaBet error: {e}")
    return odds


def scrape_betpawa():
    odds = []
    seen_matches = set()
    urls = [
        "https://www.betpawa.ug/events?categoryId=2&marketId=1X2",
        "https://www.betpawa.ug/events/popular",
    ]
    skip = [
        "pm", "am", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri",
        "Full Time", "Half", "1UP", "2UP", "1X2", "Double", "Both",
        "Over", "Under", "Total", "Score", "Chance", "Teams", "Interval",
        "minutes", "First"
    ]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 12; Samsung Galaxy) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                viewport={"width": 390, "height": 844},
                locale="en-UG",
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            for url in urls:
                try:
                    print(f"BetPawa: {url}")
                    page.goto(url, timeout=60000)
                    page.wait_for_timeout(6000)
                    links = page.query_selector_all('a[href*="/event/"], a[href*="/match/"]')
                    print(f"  Found {len(links)} links")
                    page_odds = 0

                    for link in links[:60]:
                        try:
                            text = link.inner_text()
                            parts = [p.strip() for p in text.split("\n") if p.strip()]
                            teams = []
                            odd_values = []
                            competition = ""

                            for part in parts:
                                if re.match(r"^\d+\.\d+$", part):
                                    odd_values.append(float(part))
                                elif any(s in part for s in ["Football", "Soccer", "Netball", "Tennis", "Basketball"]):
                                    competition = part
                                elif part in ["1", "X", "2", "1X", "X2", "12"]:
                                    continue
                                elif any(s in part for s in skip):
                                    continue
                                elif re.match(r"^\d+:\d+", part):
                                    continue
                                elif re.match(r"^\d+/\d+", part):
                                    continue
                                elif len(part) > 2:
                                    teams.append(part)

                            if len(teams) >= 2 and len(odd_values) >= 2:
                                match_key = f"{teams[0]}vs{teams[1]}".lower().replace(" ", "")
                                if match_key not in seen_matches:
                                    seen_matches.add(match_key)
                                    page_odds += 1

                                    # detect sport from competition text (Football default)
                                    sport_name = "Football"
                                    comp_lower = (competition or "").lower()
                                    if "netball" in comp_lower:
                                        sport_name = "Netball"
                                    elif "basketball" in comp_lower:
                                        sport_name = "Basketball"
                                    elif "tennis" in comp_lower:
                                        sport_name = "Tennis"

                                    record = build_match_record(
                                        home_team=teams[0],
                                        away_team=teams[1],
                                        bookmaker="BetPawa",
                                        home=odd_values[0],
                                        draw=odd_values[1] if len(odd_values) >= 3 else None,
                                        away=odd_values[2] if len(odd_values) >= 3 else odd_values[1],
                                        sport=sport_name,
                                        competition=competition,
                                    )
                                    odds.append(normalize_sport_name(record))
                        except:
                            continue

                    print(f"  New matches: {page_odds}")
                except Exception as e:
                    print(f"  Page failed: {e}")

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
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Referer": "https://desktop.fortebet.ug/prematch/landing",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        inner = data.get("data", {})
        events = inner.get("event", {})
        markets = inner.get("markets", {})
        competitors = inner.get("competitors", {})
        print(f"Fortebet: {len(events)} events, {len(markets)} markets, {len(competitors)} competitors")

        event_markets = {}
        for _, market in markets.items():
            eid = str(market.get("eventId", ""))
            event_markets.setdefault(eid, []).append(market)

        football_count = 0
        for eid, event in events.items():
            try:
                comp_ids = event.get("competitors", [])
                if len(comp_ids) < 2:
                    continue

                home_team = competitors.get(str(comp_ids[0]), {}).get("name", "")
                away_team = competitors.get(str(comp_ids[1]), {}).get("name", "")
                if not home_team or not away_team:
                    continue

                h_odd = d_odd = a_odd = None
                for market in event_markets.get(eid, []):
                    if market.get("marketId") == 1:
                        mkt_odds = market.get("odds", {})
                        odd_list = []
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                odd_list.append((v.get("outcomeId", 0), clean_odd(v["odds"])))
                        odd_list = [(i, o) for i, o in odd_list if o is not None]
                        odd_list.sort(key=lambda x: x[0])

                        if len(odd_list) >= 3:
                            h_odd, d_odd, a_odd = odd_list[0][1], odd_list[1][1], odd_list[2][1]
                        elif len(odd_list) == 2:
                            h_odd, a_odd = odd_list[0][1], odd_list[1][1]
                            d_odd = None
                        break

                if h_odd is None or a_odd is None:
                    continue

                # base: draw presence
                sport_name = "Netball" if d_odd is None else "Football"

                # optional refinement if event contains sport name
                ev_sport = ""
                if isinstance(event, dict):
                    ev_sport = (event.get("sportName") or event.get("sport") or "").lower()
                if "basketball" in ev_sport:
                    sport_name = "Basketball"
                elif "tennis" in ev_sport:
                    sport_name = "Tennis"
                elif "football" in ev_sport or "soccer" in ev_sport:
                    sport_name = "Football"

                football_count += 1

                record = build_match_record(
                    home_team=home_team,
                    away_team=away_team,
                    bookmaker="Fortebet",
                    home=h_odd,
                    draw=d_odd,
                    away=a_odd,
                    sport=sport_name,
                )
                odds.append(normalize_sport_name(record))
            except:
                continue

        print(f"Fortebet: {football_count} matches extracted")
    except Exception as e:
        print(f"Fortebet error: {e}")
    return odds


def scrape_sportybet():
    odds = []
    try:
        print("Fetching SportyBet from Replit API...")
        req = urllib.request.Request(
            SPORTYBET_API,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        if isinstance(data, list):
            sport_counts = {}
            for event in data:
                try:
                    home = event.get("home_team", "")
                    away = event.get("away_team", "")
                    if not home or not away:
                        continue

                    sport = (event.get("sport") or "Football").strip()
                    h_odd = clean_odd(event.get("home"))
                    d_odd = clean_odd(event.get("draw"))
                    a_odd = clean_odd(event.get("away"))

                    if h_odd is not None and a_odd is not None:
                        sport_counts[sport] = sport_counts.get(sport, 0) + 1
                        record = build_match_record(
                            home_team=home,
                            away_team=away,
                            bookmaker="SportyBet",
                            home=h_odd,
                            draw=d_odd,
                            away=a_odd,
                            sport=sport,
                        )
                        odds.append(normalize_sport_name(record))
                except:
                    continue

            print(f"SportyBet sports breakdown: {sport_counts}")
        print(f"SportyBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"SportyBet error: {e}")
    return odds


# NEW: Betika native API scraper
def scrape_betika():
    odds = []
    try:
        print("Fetching Betika Uganda API...")
        url = (
            "https://api-ug.betika.com/v1/uo/matches"
            "?page=1&limit=200&tab=&sub_type_id=1&sport_id=3&sort_id=1&period_id=-1&esports=false"
        )
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Referer": "https://www.betika.com/en-ug/",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        matches = data.get("data", []) if isinstance(data, dict) else []
        count = 0

        for m in matches:
            try:
                home_team = m.get("home_team") or ""
                away_team = m.get("away_team") or ""
                if not home_team or not away_team:
                    continue

                markets = m.get("odds") or m.get("sub_types") or []
                home_odd = draw_odd = away_odd = None

                for market in markets:
                    if str(market.get("sub_type_id")) != "1":
                        continue

                    for sel in market.get("odds", []):
                        outcome = (sel.get("odd_type") or sel.get("name") or "").strip()
                        price = clean_odd(sel.get("value") or sel.get("odd_value"))
                        if price is None:
                            continue

                        if outcome in ("1", "Home"):
                            home_odd = price
                        elif outcome in ("X", "Draw"):
                            draw_odd = price
                        elif outcome in ("2", "Away"):
                            away_odd = price

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(
                        build_match_record(
                            home_team=home_team,
                            away_team=away_team,
                            bookmaker="Betika",
                            home=home_odd,
                            draw=draw_odd,
                            away=away_odd,
                            sport="Football",
                            competition=m.get("competition_name", ""),
                        )
                    )
            except Exception as e:
                print(f"Betika match error: {e}")
                continue

        print(f"Betika: {count} matches extracted")
    except Exception as e:
        print(f"Betika error: {e}")
    return odds


def scrape_1xbet():
    odds = []
    try:
        print("Fetching 1xBet Uganda...")
        url = "https://1xbet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=135&getEmpty=true&virtualSports=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except:
                data = json.loads(raw.decode("utf-8-sig"))

        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue

                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        home_odd = c
                    elif t == "2":
                        away_odd = c
                    elif t == "3":
                        draw_odd = c

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(build_match_record(
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker="1xBet",
                        home=home_odd,
                        draw=draw_odd,
                        away=away_odd,
                        sport="Football",
                    ))
            except:
                continue

        print(f"1xBet: {count} matches extracted")
    except Exception as e:
        print(f"1xBet error: {e}")
    return odds


def scrape_22bet():
    odds = []
    try:
        print("Fetching 22Bet Uganda...")
        url = "https://22bet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=151&getEmpty=true&virtualSports=true"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except:
                data = json.loads(raw.decode("utf-8-sig"))

        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue

                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        home_odd = c
                    elif t == "2":
                        away_odd = c
                    elif t == "3":
                        draw_odd = c

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(build_match_record(
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker="22Bet",
                        home=home_odd,
                        draw=draw_odd,
                        away=away_odd,
                        sport="Football",
                    ))
            except:
                continue

        print(f"22Bet: {count} matches extracted")
    except Exception as e:
        print(f"22Bet error: {e}")
    return odds


def scrape_melbet():
    odds = []
    try:
        print("Fetching Melbet...")
        url = "https://melbet-046935.top/service-api/LineFeed/Get1x2_VZip?count=1000&lng=en&mode=4&country=191&partner=8&getEmpty=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-mobile-project-id": "0",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except:
                data = json.loads(raw.decode("utf-8-sig"))

        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue

                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        home_odd = c
                    elif t == "2":
                        away_odd = c
                    elif t == "3":
                        draw_odd = c

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(build_match_record(
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker="Melbet",
                        home=home_odd,
                        draw=draw_odd,
                        away=away_odd,
                        sport="Football",
                    ))
            except:
                continue

        print(f"Melbet: {count} matches extracted")
    except Exception as e:
        print(f"Melbet error: {e}")
    return odds


# >>> NEW: GSB Uganda scraper <<<
def scrape_gsb():
    """
    Scrape GSB Uganda prematch soccer odds from:
    https://gsb.ug/services/evapi/event/GetEvents

    Uses sportTypeIds=31 (Soccer) and statusId=0 (active events).
    Returns a list of build_match_record dicts.
    """
    odds = []
    try:
        print("Fetching GSB Uganda GetEvents...")

        base_url = "https://gsb.ug/services/evapi/event/GetEvents"
        params = {
            "timestamp": str(int(datetime.utcnow().timestamp() * 1000)),
            "betTypeIds": "-1",   # all bet types
            "sportTypeIds": "31", # soccer
            "statusId": "0",      # only active events
        }
        query = urllib.parse.urlencode(params)
        url = f"{base_url}?{query}"

        headers = {
            "Accept": "*/*, application/json",
            "Content-Type": "application/json",
            "BrandId": "112",
            "ChannelId": "4",
            "Language": "en-US",
            "Terminal": "gsb.ug",
            "User-Agent": "Mozilla/5.0",
        }

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            data = json.loads(raw.decode("utf-8"))

        events = data.get("data", []) or []
        print(f"GSB: {len(events)} events returned")

        for ev in events:
            try:
                # Only soccer, though API is already filtered by sportTypeIds=31
                if ev.get("sid") != 31:
                    continue

                home_team = ev.get("h") or ""
                away_team = ev.get("a") or ""
                if not home_team or not away_team:
                    continue

                league_name = ev.get("ln") or ""
                sport_name = ev.get("sn") or "Football"

                # Extract FT 1X2
                home_odd = draw_odd = away_odd = None
                for bt in ev.get("bts", []) or []:
                    name = (bt.get("n") or "").strip()
                    if name != "FT 1X2":
                        continue

                    for o in bt.get("odds", []) or []:
                        sel = (o.get("n") or "").strip()
                        price = clean_odd(o.get("p"))
                        if price is None:
                            continue
                        if sel == "1":
                            home_odd = price
                        elif sel == "X":
                            draw_odd = price
                        elif sel == "2":
                            away_odd = price

                if home_odd is None or away_odd is None:
                    continue

                odds.append(
                    build_match_record(
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker="GSB",
                        home=home_odd,
                        draw=draw_odd,
                        away=away_odd,
                        sport=sport_name,
                        competition=league_name,
                    )
                )
            except Exception as e:
                print(f"GSB event error: {e}")
                continue

        print(f"GSB: {len(odds)} matches extracted")
    except Exception as e:
        print(f"GSB error: {e}")

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

        merged_groups = {}
        processed_keys = set()
        all_keys = list(exact_groups.keys())

        for i, key1 in enumerate(all_keys):
            if key1 in processed_keys:
                continue
            group = list(exact_groups[key1])
            processed_keys.add(key1)

            for key2 in all_keys[i + 1:]:
                if key2 in processed_keys:
                    continue
                if match_key_similarity(key1, key2):
                    group.extend(exact_groups[key2])
                    processed_keys.add(key2)

            merged_groups[key1] = group

        for match_name, bookmakers in merged_groups.items():
            bookie_names = set(b["bookmaker"] for b in bookmakers)
            if len(bookie_names) < 2:
                continue

            bk_odds = {}
            for b in bookmakers:
                bk = b["bookmaker"]
                bk_odds.setdefault(bk, {"home": 0.0, "draw": 0.0, "away": 0.0})

                home = clean_odd(b.get("home"))
                draw = clean_odd(b.get("draw"))
                away = clean_odd(b.get("away"))

                if home is not None and home > bk_odds[bk]["home"]:
                    bk_odds[bk]["home"] = home
                if draw is not None and draw > bk_odds[bk]["draw"]:
                    bk_odds[bk]["draw"] = draw
                if away is not None and away > bk_odds[bk]["away"]:
                    bk_odds[bk]["away"] = away

            bk_list = list(bk_odds.keys())

            if sport in ["Football", "Rugby", "Futsal"]:
                best = None
                for bk_h in bk_list:
                    for bk_d in bk_list:
                        for bk_a in bk_list:
                            if bk_h == bk_d or bk_h == bk_a or bk_d == bk_a:
                                continue

                            h = bk_odds[bk_h]["home"]
                            d = bk_odds[bk_d]["draw"]
                            a = bk_odds[bk_a]["away"]
                            if not h or not d or not a:
                                continue

                            arb = (1 / h) + (1 / d) + (1 / a)
                            if arb < 1:
                                profit = round((1 - arb) * 100, 2)
                                if 0.5 <= profit <= 20.0:
                                    if best is None or profit > best["profit_percent"]:
                                        stake_h = round(STAKE * (1 / h) / arb)
                                        stake_d = round(STAKE * (1 / d) / arb)
                                        stake_a = round(STAKE * (1 / a) / arb)
                                        best = {
                                            "match": match_name,
                                            "sport": sport,
                                            "type": "3-way",
                                            "profit_percent": profit,
                                            "profit_ugx": round(STAKE * (1 - arb)),
                                            "total_stake": STAKE,
                                            "arb_sum": round(arb, 4),
                                            "bets": [
                                                {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stake_h, "win": round(stake_h * h)},
                                                {"bookmaker": bk_d, "outcome": "Draw", "odd": d, "stake": stake_d, "win": round(stake_d * d)},
                                                {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stake_a, "win": round(stake_a * a)},
                                            ],
                                        }

                if best:
                    opportunities.append(best)

            else:
                best = None
                for bk_h in bk_list:
                    for bk_a in bk_list:
                        if bk_h == bk_a:
                            continue

                        h = bk_odds[bk_h]["home"]
                        a = bk_odds[bk_a]["away"]
                        if not h or not a:
                            continue

                        arb = (1 / h) + (1 / a)
                        if arb < 1:
                            profit = round((1 - arb) * 100, 2)
                            if 0.5 <= profit <= 20.0:
                                if best is None or profit > best["profit_percent"]:
                                    stake_h = round(STAKE * (1 / h) / arb)
                                    stake_a = round(STAKE * (1 / a) / arb)
                                    best = {
                                        "match": match_name,
                                        "sport": sport,
                                        "type": "2-way",
                                        "profit_percent": profit,
                                        "profit_ugx": round(STAKE * (1 - arb)),
                                        "total_stake": STAKE,
                                        "arb_sum": round(arb, 4),
                                        "bets": [
                                            {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stake_h, "win": round(stake_h * h)},
                                            {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stake_a, "win": round(stake_a * a)},
                                        ],
                                    }

                if best:
                    opportunities.append(best)

    return sorted(opportunities, key=lambda x: x["profit_percent"], reverse=True)


def write_html_report(output):
    opportunities = output.get("opportunities", [])
    last_updated = output.get("last_updated", "")

    html = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        "<title>Arbitrage Opportunities</title

You want to add **history tracking and validity info** on top of this code, without changing the existing arbitrage logic. Below is a full rewrite of your script with:

- A history store that tracks each opportunity over time.
- `first_seen`, `last_seen`, `valid` flags.
- A versions list storing profit/arb values per scan.
- HTML updated to show validity and timestamps.

Everything else (scrapers, find_arbitrage math, stake logic) is left intact, except for the minimal wiring to plug in the history.

```python
import json
import re
import time
import urllib.parse          # <-- added for GSB scraper
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

# Shared 1xBet-style backend family configuration
SHARED_BOOKMAKERS_1X = {
    "1xBet": {
        "base_url": "https://1xbet.ug",
        "partner": "135",
    },
    "22Bet": {
        "base_url": "https://22bet.ug",
        "partner": "151",
    },
    "Melbet": {
        "base_url": "https://melbet.ug",
        "partner": "8",
    },
    # Add additional clones here (e.g. BetWinner) once confirmed
}


def normalize(name):
    name = (name or "").lower().strip()
    name = re.sub(r"\b(fc|sc|cf|ac|united|city|sports|club|utd|football|soccer|women|men|u21|u23)\b", "", name)
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


# NEW: small helper to normalize sport names across scrapers
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


# NEW: history load/save
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
    # Key per match/sport/type
    return f"{opp['sport']}::{opp['type']}::{opp['match']}"


def update_arbitrage_history(current_opportunities, arb_history, timestamp_str):
    # Mark existing entries as not updated this cycle
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
                "versions": [],
            }
            arb_history[key] = entry

        entry = arb_history[key]
        entry["last_seen"] = timestamp_str
        entry["valid"] = True
        entry["updated_this_cycle"] = True

        version = {
            "timestamp": timestamp_str,
            "profit_percent": opp["profit_percent"],
            "profit_ugx": opp["profit_ugx"],
            "arb_sum": opp["arb_sum"],
            "bets": deepcopy(opp["bets"]),
        }
        entry["versions"].append(version)

    # Any entry not updated in this cycle is now invalid
    for key, entry in arb_history.items():
        if not entry.get("updated_this_cycle"):
            entry["valid"] = False

    # Clean up helper flag
    for entry in arb_history.values():
        if "updated_this_cycle" in entry:
            del entry["updated_this_cycle"]


# NEW: use detailed match betMap
def championbet_extract_1x2_from_betmap(bet_map):
    """
    Given a betMap dict from the detailed match endpoint, return (home, draw, away) odds.
    Uses your existing assumption: tt 1/4/7 = Home, 2/5/8 = Draw, 3/6/9 = Away.
    """
    bet_map = bet_map or {}

    def pick_odd(market_keys):
        for k in market_keys:
            market = bet_map.get(str(k), {}) or {}
            if not isinstance(market, dict):
                continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None:
                        return odd
        return None

    home = pick_odd([1, 4, 7])
    draw = pick_odd([2, 5, 8])
    away = pick_odd([3, 6, 9])
    return home, draw, away


def scrape_championbet():
    odds = []
    try:
        print("Fetching ChampionBet top list...")
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
                if "Soccer" not in sport_token:
                    continue

                match_id = m.get("id")
                if not match_id:
                    continue

                home_team = m.get("home") or ""
                away_team = m.get("away") or ""
                if not home_team or not away_team:
                    continue

                match_url = CHAMPIONBET_MATCH_API.format(match_id=match_id)
                match_req = urllib.request.Request(match_url, headers=headers)
                with urllib.request.urlopen(match_req, timeout=30) as r2:
                    match_data = json.loads(r2.read().decode())

                bet_map = match_data.get("betMap", {}) if isinstance(match_data, dict) else {}
                home_odd, draw_odd, away_odd = championbet_extract_1x2_from_betmap(bet_map)

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(
                        build_match_record(
                            home_team=home_team,
                            away_team=away_team,
                            bookmaker="ChampionBet",
                            home=home_odd,
                            draw=draw_odd,
                            away=away_odd,
                            sport="Football",
                            competition=m.get("leagueName", ""),
                        )
                    )

                time.sleep(0.2)

            except Exception as e:
                print(f"ChampionBet match error: {e}")
                continue

        print(f"ChampionBet: {count} matches extracted with detailed betMap")
    except Exception as e:
        print(f"ChampionBet error: {e}")
    return odds


def scrape_ababet():
    odds = []
    try:
        print("Fetching AbaBet...")
        url = "https://www.ababet.ug/soccer/match_result?mobile=1"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            print("AbaBet: no tables found")
            return odds

        for table in tables:
            first_row = table.find("tr")
            if not first_row:
                continue

            table_headers = [c.get_text(" ", strip=True) for c in first_row.find_all(["th", "td"])]
            if not table_headers or "Home" not in table_headers or "Away" not in table_headers:
                continue

            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 5:
                    continue

                row = dict(zip(table_headers, cells[:len(table_headers)]))
                home_team = row.get("Home")
                away_team = row.get("Away")
                if not home_team or not away_team or home_team == "-" or away_team == "-":
                    continue

                odds.append(build_match_record(
                    home_team=home_team,
                    away_team=away_team,
                    bookmaker="AbaBet",
                    home=row.get("1"),
                    draw=row.get("X"),
                    away=row.get("2"),
                    sport="Football",
                    competition=row.get("League", "")
                ))

        print(f"AbaBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"AbaBet error: {e}")
    return odds


def scrape_betpawa():
    odds = []
    seen_matches = set()
    urls = [
        "https://www.betpawa.ug/events?categoryId=2&marketId=1X2",
        "https://www.betpawa.ug/events/popular",
    ]
    skip = [
        "pm", "am", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri",
        "Full Time", "Half", "1UP", "2UP", "1X2", "Double", "Both",
        "Over", "Under", "Total", "Score", "Chance", "Teams", "Interval",
        "minutes", "First"
    ]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 12; Samsung Galaxy) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                viewport={"width": 390, "height": 844},
                locale="en-UG",
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            for url in urls:
                try:
                    print(f"BetPawa: {url}")
                    page.goto(url, timeout=60000)
                    page.wait_for_timeout(6000)
                    links = page.query_selector_all('a[href*="/event/"], a[href*="/match/"]')
                    print(f"  Found {len(links)} links")
                    page_odds = 0

                    for link in links[:60]:
                        try:
                            text = link.inner_text()
                            parts = [p.strip() for p in text.split("\n") if p.strip()]
                            teams = []
                            odd_values = []
                            competition = ""

                            for part in parts:
                                if re.match(r"^\d+\.\d+$", part):
                                    odd_values.append(float(part))
                                elif any(s in part for s in ["Football", "Soccer", "Netball", "Tennis", "Basketball"]):
                                    competition = part
                                elif part in ["1", "X", "2", "1X", "X2", "12"]:
                                    continue
                                elif any(s in part for s in skip):
                                    continue
                                elif re.match(r"^\d+:\d+", part):
                                    continue
                                elif re.match(r"^\d+/\d+", part):
                                    continue
                                elif len(part) > 2:
                                    teams.append(part)

                            if len(teams) >= 2 and len(odd_values) >= 2:
                                match_key = f"{teams[0]}vs{teams[1]}".lower().replace(" ", "")
                                if match_key not in seen_matches:
                                    seen_matches.add(match_key)
                                    page_odds += 1

                                    sport_name = "Football"
                                    comp_lower = (competition or "").lower()
                                    if "netball" in comp_lower:
                                        sport_name = "Netball"
                                    elif "basketball" in comp_lower:
                                        sport_name = "Basketball"
                                    elif "tennis" in comp_lower:
                                        sport_name = "Tennis"

                                    record = build_match_record(
                                        home_team=teams[0],
                                        away_team=teams[1],
                                        bookmaker="BetPawa",
                                        home=odd_values[0],
                                        draw=odd_values[1] if len(odd_values) >= 3 else None,
                                        away=odd_values[2] if len(odd_values) >= 3 else odd_values[1],
                                        sport=sport_name,
                                        competition=competition,
                                    )
                                    odds.append(normalize_sport_name(record))
                        except:
                            continue

                    print(f"  New matches: {page_odds}")
                except Exception as e:
                    print(f"  Page failed: {e}")

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
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Referer": "https://desktop.fortebet.ug/prematch/landing",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        inner = data.get("data", {})
        events = inner.get("event", {})
        markets = inner.get("markets", {})
        competitors = inner.get("competitors", {})
        print(f"Fortebet: {len(events)} events, {len(markets)} markets, {len(competitors)} competitors")

        event_markets = {}
        for _, market in markets.items():
            eid = str(market.get("eventId", ""))
            event_markets.setdefault(eid, []).append(market)

        football_count = 0
        for eid, event in events.items():
            try:
                comp_ids = event.get("competitors", [])
                if len(comp_ids) < 2:
                    continue

                home_team = competitors.get(str(comp_ids[0]), {}).get("name", "")
                away_team = competitors.get(str(comp_ids[1]), {}).get("name", "")
                if not home_team or not away_team:
                    continue

                h_odd = d_odd = a_odd = None
                for market in event_markets.get(eid, []):
                    if market.get("marketId") == 1:
                        mkt_odds = market.get("odds", {})
                        odd_list = []
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                odd_list.append((v.get("outcomeId", 0), clean_odd(v["odds"])))
                        odd_list = [(i, o) for i, o in odd_list if o is not None]
                        odd_list.sort(key=lambda x: x[0])

                        if len(odd_list) >= 3:
                            h_odd, d_odd, a_odd = odd_list[0][1], odd_list[1][1], odd_list[2][1]
                        elif len(odd_list) == 2:
                            h_odd, a_odd = odd_list[0][1], odd_list[1][1]
                            d_odd = None
                        break

                if h_odd is None or a_odd is None:
                    continue

                sport_name = "Netball" if d_odd is None else "Football"

                ev_sport = ""
                if isinstance(event, dict):
                    ev_sport = (event.get("sportName") or event.get("sport") or "").lower()
                if "basketball" in ev_sport:
                    sport_name = "Basketball"
                elif "tennis" in ev_sport:
                    sport_name = "Tennis"
                elif "football" in ev_sport or "soccer" in ev_sport:
                    sport_name = "Football"

                football_count += 1

                record = build_match_record(
                    home_team=home_team,
                    away_team=away_team,
                    bookmaker="Fortebet",
                    home=h_odd,
                    draw=d_odd,
                    away=a_odd,
                    sport=sport_name,
                )
                odds.append(normalize_sport_name(record))
            except:
                continue

        print(f"Fortebet: {football_count} matches extracted")
    except Exception as e:
        print(f"Fortebet error: {e}")
    return odds


def scrape_sportybet():
    odds = []
    try:
        print("Fetching SportyBet from Replit API...")
        req = urllib.request.Request(
            SPORTYBET_API,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        if isinstance(data, list):
            sport_counts = {}
            for event in data:
                try:
                    home = event.get("home_team", "")
                    away = event.get("away_team", "")
                    if not home or not away:
                        continue

                    sport = (event.get("sport") or "Football").strip()
                    h_odd = clean_odd(event.get("home"))
                    d_odd = clean_odd(event.get("draw"))
                    a_odd = clean_odd(event.get("away"))

                    if h_odd is not None and a_odd is not None:
                        sport_counts[sport] = sport_counts.get(sport, 0) + 1
                        record = build_match_record(
                            home_team=home,
                            away_team=away,
                            bookmaker="SportyBet",
                            home=h_odd,
                            draw=d_odd,
                            away=a_odd,
                            sport=sport,
                        )
                        odds.append(normalize_sport_name(record))
                except:
                    continue

            print(f"SportyBet sports breakdown: {sport_counts}")
        print(f"SportyBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"SportyBet error: {e}")
    return odds


# NEW: Betika native API scraper
def scrape_betika():
    odds = []
    try:
        print("Fetching Betika Uganda API...")
        url = (
            "https://api-ug.betika.com/v1/uo/matches"
            "?page=1&limit=200&tab=&sub_type_id=1&sport_id=3&sort_id=1&period_id=-1&esports=false"
        )
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Referer": "https://www.betika.com/en-ug/",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        matches = data.get("data", []) if isinstance(data, dict) else []
        count = 0

        for m in matches:
            try:
                home_team = m.get("home_team") or ""
                away_team = m.get("away_team") or ""
                if not home_team or not away_team:
                    continue

                markets = m.get("odds") or m.get("sub_types") or []
                home_odd = draw_odd = away_odd = None

                for market in markets:
                    if str(market.get("sub_type_id")) != "1":
                        continue

                    for sel in market.get("odds", []):
                        outcome = (sel.get("odd_type") or sel.get("name") or "").strip()
                        price = clean_odd(sel.get("value") or sel.get("odd_value"))
                        if price is None:
                            continue

                        if outcome in ("1", "Home"):
                            home_odd = price
                        elif outcome in ("X", "Draw"):
                            draw_odd = price
                        elif outcome in ("2", "Away"):
                            away_odd = price

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(
                        build_match_record(
                            home_team=home_team,
                            away_team=away_team,
                            bookmaker="Betika",
                            home=home_odd,
                            draw=draw_odd,
                            away=away_odd,
                            sport="Football",
                            competition=m.get("competition_name", ""),
                        )
                    )
            except Exception as e:
                print(f"Betika match error: {e}")
                continue

        print(f"Betika: {count} matches extracted")
    except Exception as e:
        print(f"Betika error: {e}")
    return odds


def scrape_1xbet():
    odds = []
    try:
        print("Fetching 1xBet Uganda...")
        url = "https://1xbet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=135&getEmpty=true&virtualSports=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except:
                data = json.loads(raw.decode("utf-8-sig"))

        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue

                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        home_odd = c
                    elif t == "2":
                        away_odd = c
                    elif t == "3":
                        draw_odd = c

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(build_match_record(
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker="1xBet",
                        home=home_odd,
                        draw=draw_odd,
                        away=away_odd,
                        sport="Football",
                    ))
            except:
                continue

        print(f"1xBet: {count} matches extracted")
    except Exception as e:
        print(f"1xBet error: {e}")
    return odds


def scrape_22bet():
    odds = []
    try:
        print("Fetching 22Bet Uganda...")
        url = "https://22bet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=151&getEmpty=true&virtualSports=true"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except:
                data = json.loads(raw.decode("utf-8-sig"))

        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue

                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        home_odd = c
                    elif t == "2":
                        away_odd = c
                    elif t == "3":
                        draw_odd = c

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(build_match_record(
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker="22Bet",
                        home=home_odd,
                        draw=draw_odd,
                        away=away_odd,
                        sport="Football",
                    ))
            except:
                continue

        print(f"22Bet: {count} matches extracted")
    except Exception as e:
        print(f"22Bet error: {e}")
    return odds


def scrape_melbet():
    odds = []
    try:
        print("Fetching Melbet...")
        url = "https://melbet-046935.top/service-api/LineFeed/Get1x2_VZip?count=1000&lng=en&mode=4&country=191&partner=8&getEmpty=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-mobile-project-id": "0",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except:
                data = json.loads(raw.decode("utf-8-sig"))

        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue

                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        home_odd = c
                    elif t == "2":
                        away_odd = c
                    elif t == "3":
                        draw_odd = c

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(build_match_record(
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker="Melbet",
                        home=home_odd,
                        draw=draw_odd,
                        away=away_odd,
                        sport="Football",
                    ))
            except:
                continue

        print(f"Melbet: {count} matches extracted")
    except Exception as e:
        print(f"Melbet error: {e}")
    return odds


# >>> NEW: GSB Uganda scraper <<<
def scrape_gsb():
    """
    Scrape GSB Uganda prematch soccer odds from:
    https://gsb.ug/services/evapi/event/GetEvents

    Uses sportTypeIds=31 (Soccer) and statusId=0 (active events).
    Returns a list of build_match_record dicts.
    """
    odds = []
    try:
        print("Fetching GSB Uganda GetEvents...")

        base_url = "https://gsb.ug/services/evapi/event/GetEvents"
        params = {
            "timestamp": str(int(datetime.utcnow().timestamp() * 1000)),
            "betTypeIds": "-1",   # all bet types
            "sportTypeIds": "31", # soccer
            "statusId": "0",      # only active events
        }
        query = urllib.parse.urlencode(params)
        url = f"{base_url}?{query}"

        headers = {
            "Accept": "*/*, application/json",
            "Content-Type": "application/json",
            "BrandId": "112",
            "ChannelId": "4",
            "Language": "en-US",
            "Terminal": "gsb.ug",
            "User-Agent": "Mozilla/5.0",
        }

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            data = json.loads(raw.decode("utf-8"))

        events = data.get("data", []) or []
        print(f"GSB: {len(events)} events returned")

        for ev in events:
            try:
                if ev.get("sid") != 31:
                    continue

                home_team = ev.get("h") or ""
                away_team = ev.get("a") or ""
                if not home_team or not away_team:
                    continue

                league_name = ev.get("ln") or ""
                sport_name = ev.get("sn") or "Football"

                home_odd = draw_odd = away_odd = None
                for bt in ev.get("bts", []) or []:
                    name = (bt.get("n") or "").strip()
                    if name != "FT 1X2":
                        continue

                    for o in bt.get("odds", []) or []:
                        sel = (o.get("n") or "").strip()
                        price = clean_odd(o.get("p"))
                        if price is None:
                            continue
                        if sel == "1":
                            home_odd = price
                        elif sel == "X":
                            draw_odd = price
                        elif sel == "2":
                            away_odd = price

                if home_odd is None or away_odd is None:
                    continue

                odds.append(
                    build_match_record(
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker="GSB",
                        home=home_odd,
                        draw=draw_odd,
                        away=away_odd,
                        sport=sport_name,
                        competition=league_name,
                    )
                )
            except Exception as e:
                print(f"GSB event error: {e}")
                continue

        print(f"GSB: {len(odds)} matches extracted")
    except Exception as e:
        print(f"GSB error: {e}")

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

        merged_groups = {}
        processed_keys = set()
        all_keys = list(exact_groups.keys())

        for i, key1 in enumerate(all_keys):
            if key1 in processed_keys:
                continue
            group = list(exact_groups[key1])
            processed_keys.add(key1)

            for key2 in all_keys[i + 1:]:
                if key2 in processed_keys:
                    continue
                if match_key_similarity(key1, key2):
                    group.extend(exact_groups[key2])
                    processed_keys.add(key2)

            merged_groups[key1] = group

        for match_name, bookmakers in merged_groups.items():
            bookie_names = set(b["bookmaker"] for b in bookmakers)
            if len(bookie_names) < 2:
                continue

            bk_odds = {}
            for b in bookmakers:
                bk = b["bookmaker"]
                bk_odds.setdefault(bk, {"home": 0.0, "draw": 0.0, "away": 0.0})

                home = clean_odd(b.get("home"))
                draw = clean_odd(b.get("draw"))
                away = clean_odd(b.get("away"))

                if home is not None and home > bk_odds[bk]["home"]:
                    bk_odds[bk]["home"] = home
                if draw is not None and draw > bk_odds[bk]["draw"]:
                    bk_odds[bk]["draw"] = draw
                if away is not None and away > bk_odds[bk]["away"]:
                    bk_odds[bk]["away"] = away

            bk_list = list(bk_odds.keys())

            if sport in ["Football", "Rugby", "Futsal"]:
                best = None
                for bk_h in bk_list:
                    for bk_d in bk_list:
                        for bk_a in bk_list:
                            if bk_h == bk_d or bk_h == bk_a or bk_d == bk_a:
                                continue

                            h = bk_odds[bk_h]["home"]
                            d = bk_odds[bk_d]["draw"]
                            a = bk_odds[bk_a]["away"]
                            if not h or not d or not a:
                                continue

                            arb = (1 / h) + (1 / d) + (1 / a)
                            if arb < 1:
                                profit = round((1 - arb) * 100, 2)
                                if 0.5 <= profit <= 20.0:
                                    if best is None or profit > best["profit_percent"]:
                                        stake_h = round(STAKE * (1 / h) / arb)
                                        stake_d = round(STAKE * (1 / d) / arb)
                                        stake_a = round(STAKE * (1 / a) / arb)
                                        best = {
                                            "match": match_name,
                                            "sport": sport,
                                            "type": "3-way",
                                            "profit_percent": profit,
                                            "profit_ugx": round(STAKE * (1 - arb)),
                                            "total_stake": STAKE,
                                            "arb_sum": round(arb, 4),
                                            "bets": [
                                                {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stake_h, "win": round(stake_h * h)},
                                                {"bookmaker": bk_d, "outcome": "Draw", "odd": d, "stake": stake_d, "win": round(stake_d * d)},
                                                {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stake_a, "win": round(stake_a * a)},
                                            ],
                                        }

                if best:
                    opportunities.append(best)

            else:
                best = None
                for bk_h in bk_list:
                    for bk_a in bk_list:
                        if bk_h == bk_a:
                            continue

                        h = bk_odds[bk_h]["home"]
                        a = bk_odds[bk_a]["away"]
                        if not h or not a:
                            continue

                        arb = (1 / h) + (1 / a)
                        if arb < 1:
                            profit = round((1 - arb) * 100, 2)
                            if 0.5 <= profit <= 20.0:
                                if best is None or profit > best["profit_percent"]:
                                    stake_h = round(STAKE * (1 / h) / arb)
                                    stake_a = round(STAKE * (1 / a) / arb)
                                    best = {
                                        "match": match_name,
                                        "sport": sport,
                                        "type": "2-way",
                                        "profit_percent": profit,
                                        "profit_ugx": round(STAKE * (1 - arb)),
                                        "total_stake": STAKE,
                                        "arb_sum": round(arb, 4),
                                        "bets": [
                                            {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stake_h, "win": round(stake_h * h)},
                                            {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stake_a, "win": round(stake_a * a)},
                                        ],
                                    }

                if best:
                    opportunities.append(best)

    return sorted(opportunities, key=lambda x: x["profit_percent"], reverse=True)


def write_html_report(output, arb_history=None):
    opportunities = output.get("opportunities", [])
    last_updated = output.get("last_updated", "")

    if arb_history is None:
        arb_history = {}

    html = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        "<title>Arbitrage Opportunities</title>",
        "<style>",
        "body { font-family: Arial, sans-serif; margin: 20px; }",
        "table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }",
        "th, td { border: 1px solid #ccc; padding: 6px; font-size: 14px; }",
        "th { background: #f0f0f0; }",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>Arbitrage Opportunities</h1>",
        f"<p>Last updated: {last_updated}</p>",
    ]

    if not opportunities:
        html.append("<p>No opportunities found.</p>")
    else:
        html.append("<table>")
        html.append("<tr><th>Match</th><th>Sport</th><th>Type</th><th>Profit %</th><th>Profit UGX</th><th>Valid</th><th>First Seen</th><th>Last Seen</th><th>Bets</th></tr>")
        for opp in opportunities[:50]:
            key = opportunity_key(opp)
            hist = arb_history.get(key, {})
            valid = hist.get("valid", True)
            first_seen = hist.get("first_seen", "")
            last_seen = hist.get("last_seen", "")

            bets_text = "; ".join(
                f"{b['bookmaker']} {b['outcome']} @ {b['odd']}"
                for b in opp.get("bets", [])
            )
            html.append(
                f"<tr>"
                f"<td>{opp.get('match')}</td>"
                f"<td>{opp.get('sport')}</td>"
                f"<td>{opp.get('type')}</td>"
                f"<td>{opp.get('profit_percent')}%</td>"
                f"<td>{opp.get('profit_ugx')}</td>"
                f"<td>{'Yes' if valid else 'No'}</td>"
                f"<td>{first_seen}</td>"
                f"<td>{last_seen}</td>"
                f"<td>{bets_text}</td>"
                f"</tr>"
            )
        html.append("</table>")

    html.append("</body></html>")

    with open("odds.html", "w", encoding="utf-8") as f:
        f.write("\n".join(html))


def scrape_shared_1xbet_family(bookmaker_name, config):
    """
    Generic scraper for 1xBet-style APIs (1xBet, 22Bet, Melbet, etc.).
    Uses /service-api/LineFeed/Get1x2_VZip and normalizes with build_match_record().
    """
    odds = []
    base_url = config["base_url"].rstrip("/")
    partner = config["partner"]

    api_url = (
        f"{base_url}/service-api/LineFeed/Get1x2_VZip?"
        f"sports=1&count=1000&lng=en&mode=4&country=191&partner={partner}&getEmpty=true&virtualSports=true"
    )

    try:
        print(f"Fetching {bookmaker_name} via shared 1xBet-family scraper...")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
        }
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = json.loads(raw.decode("utf-8-sig"))

        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue

                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        home_odd = c
                    elif t == "2":
                        away_odd = c
                    elif t == "3":
                        draw_odd = c

                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(
                        build_match_record(
                            home_team=home_team,
                            away_team=away_team,
                            bookmaker=bookmaker_name,
                            home=home_odd,
                            draw=draw_odd,
                            away=away_odd,
                            sport="Football",
                        )
                    )
            except Exception:
                continue

        print(f"{bookmaker_name}: {count} matches extracted")
    except Exception as e:
        print(f"{bookmaker_name} error: {e}")
    return odds


def main():
    all_odds = []
    all_odds.extend(scrape_championbet())
    all_odds.extend(scrape_ababet())
    all_odds.extend(scrape_betpawa())
    all_odds.extend(scrape_fortebet())
    all_odds.extend(scrape_sportybet())
    all_odds.extend(scrape_betika())
    all_odds.extend(scrape_1xbet())
    all_odds.extend(scrape_22bet())
    all_odds.extend(scrape_melbet())
    all_odds.extend(scrape_gsb())

    # If you want to use shared 1xBet-family scrapers:
    # for name, cfg in SHARED_BOOKMAKERS_1X.items():
    #     all_odds.extend(scrape_shared_1xbet_family(name, cfg))

    all_odds = [normalize_sport_name(rec) for rec in all_odds]

    opportunities = find_arbitrage(all_odds)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    arb_history = load_arbitrage_history()
    update_arbitrage_history(opportunities, arb_history, now_str)
    save_arbitrage_history(arb_history)

    output = {
        "opportunities": opportunities,
        "last_updated": now_str,
    }
    write_html_report(output, arb_history)


if __name__ == "__main__":
    main()
