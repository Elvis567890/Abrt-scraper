import json
import os
import re
import urllib.request
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SPORTYBET_API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"
CHAMPIONBET_API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
BETIKA_API = "https://api-ug.betika.com/v1/uo/matches?page=1&limit=10&tab=&sub_type_id=1,186,340&sport_id=3&sort_id=1&period_id=-1&esports=false"
HISTORY_FILE = "arbitrage_history.json"
STAKE = 100000


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
    p1 = key1.split(" vs ")
    p2 = key2.split(" vs ")
    if len(p1) != 2 or len(p2) != 2:
        return False
    return teams_match(p1[0], p2[0]) and teams_match(p1[1], p2[1])


def clean_odd(v, min_odd=1.01, max_odd=50.0):
    try:
        if v is None:
            return None
        v = float(v)
        if min_odd <= v <= max_odd:
            return v
    except Exception:
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


def championbet_extract_1x2(match):
    bet_map = match.get("betMap", {}) or {}

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

    return pick_odd([1, 4, 7]), pick_odd([2, 5, 8]), pick_odd([3, 6, 9])


def scrape_championbet():
    odds = []
    try:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Referer": "https://www.championbet.ug/mob/",
        }
        req = urllib.request.Request(CHAMPIONBET_API, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        matches = data.get("esMatches", []) if isinstance(data, dict) else []
        for m in matches:
            try:
                sport_token = str(
                    m.get("sportToken", "")
                    or m.get("sport", "")
                    or m.get("sportName", "")
                    or m.get("sport_name", "")
                )
                if "soccer" not in sport_token.lower() and "football" not in sport_token.lower():
                    continue
                home_team = m.get("home") or m.get("homeTeam") or m.get("home_team") or m.get("team1") or ""
                away_team = m.get("away") or m.get("awayTeam") or m.get("away_team") or m.get("team2") or ""
                if not home_team or not away_team:
                    continue
                h, d, a = championbet_extract_1x2(m)
                if h is not None and a is not None:
                    odds.append(
                        build_match_record(
                            home_team,
                            away_team,
                            "ChampionBet",
                            h,
                            d,
                            a,
                            "Football",
                            m.get("leagueName", "") or m.get("competition", "") or "",
                        )
                    )
            except Exception:
                continue
    except Exception as e:
        print(f"ChampionBet error: {e}")
    return odds


def scrape_betika():
    odds = []
    try:
        req = urllib.request.Request(BETIKA_API, headers={"Accept": "application/json, text/plain, */*"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        matches = data.get("data", []) if isinstance(data, dict) else []
        for m in matches:
            try:
                home_team = m.get("home_team", "")
                away_team = m.get("away_team", "")
                if not home_team or not away_team:
                    continue
                h = clean_odd(m.get("home_odd"))
                d = clean_odd(m.get("neutral_odd"))
                a = clean_odd(m.get("away_odd"))
                if h is not None and a is not None:
                    odds.append(
                        build_match_record(
                            home_team,
                            away_team,
                            "Betika",
                            h,
                            d,
                            a,
                            m.get("sport_name", "Football"),
                            m.get("competition_name", ""),
                        )
                    )
            except Exception:
                continue
    except Exception as e:
        print(f"Betika error: {e}")
    return odds


def scrape_ababet():
    odds = []
    try:
        r = requests.get(
            "https://www.ababet.ug/soccer/match_result?mobile=1",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for table in soup.find_all("table"):
            first_row = table.find("tr")
            if not first_row:
                continue
            headers = [c.get_text(" ", strip=True) for c in first_row.find_all(["th", "td"])]
            if "Home" not in headers or "Away" not in headers:
                continue
            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 5:
                    continue
                row = dict(zip(headers, cells[: len(headers)]))
                home_team = row.get("Home")
                away_team = row.get("Away")
                if home_team and away_team and home_team != "-" and away_team != "-":
                    odds.append(
                        build_match_record(
                            home_team,
                            away_team,
                            "AbaBet",
                            row.get("1"),
                            row.get("X"),
                            row.get("2"),
                            "Football",
                            row.get("League", ""),
                        )
                    )
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
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 12; Samsung Galaxy) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                viewport={"width": 390, "height": 844},
                locale="en-UG",
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            for url in urls:
                try:
                    page.goto(url, timeout=60000)
                    page.wait_for_timeout(6000)
                    for link in page.query_selector_all('a[href*="/event/"], a[href*="/match/"]')[:60]:
                        try:
                            text = link.inner_text()
                            parts = [p.strip() for p in text.split("\n") if p.strip()]
                            teams, odd_values, competition = [], [], ""
                            for part in parts:
                                if re.match(r"^\d+\.\d+$", part):
                                    odd_values.append(float(part))
                                elif len(part) > 2 and not any(
                                    x in part for x in ["Football", "Soccer", "Netball", "Tennis", "Basketball"]
                                ):
                                    teams.append(part)
                                elif any(x in part for x in ["Football", "Soccer", "Netball", "Tennis", "Basketball"]):
                                    competition = part
                            if len(teams) >= 2 and len(odd_values) >= 2:
                                mk = f"{teams[0]}vs{teams[1]}".lower().replace(" ", "")
                                if mk not in seen_matches:
                                    seen_matches.add(mk)
                                    odds.append(
                                        build_match_record(
                                            teams[0],
                                            teams[1],
                                            "BetPawa",
                                            odd_values[0],
                                            odd_values[1] if len(odd_values) >= 3 else None,
                                            odd_values[2] if len(odd_values) >= 3 else odd_values[1],
                                            "Football",
                                            competition,
                                        )
                                    )
                        except Exception:
                            continue
                except Exception:
                    continue
            browser.close()
    except Exception as e:
        print(f"BetPawa error: {e}")
    return odds


def scrape_fortebet():
    odds = []
    try:
        url = "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://desktop.fortebet.ug/prematch/landing",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        inner = data.get("data", {})
        events = inner.get("event", {})
        markets = inner.get("markets", {})
        competitors = inner.get("competitors", {})
        event_markets = {}
        for _, market in markets.items():
            eid = str(market.get("eventId", ""))
            event_markets.setdefault(eid, []).append(market)
        for eid, event in events.items():
            try:
                comp_ids = event.get("competitors", [])
                if len(comp_ids) < 2:
                    continue
                home_team = competitors.get(str(comp_ids[0]), {}).get("name", "")
                away_team = competitors.get(str(comp_ids[1]), {}).get("name", "")
                if not home_team or not away_team:
                    continue
                h = d = a = None
                for market in event_markets.get(eid, []):
                    if market.get("marketId") == 1:
                        odds_map = market.get("odds", {})
                        odd_list = []
                        for _, v in odds_map.items():
                            if isinstance(v, dict) and "odds" in v:
                                odd_list.append((v.get("outcomeId", 0), clean_odd(v["odds"])))
                        odd_list = [(i, o) for i, o in odd_list if o is not None]
                        odd_list.sort(key=lambda x: x[0])
                        if len(odd_list) >= 3:
                            h, d, a = odd_list[0][1], odd_list[1][1], odd_list[2][1]
                        elif len(odd_list) == 2:
                            h, a = odd_list[0][1], odd_list[1][1]
                        break
                if h is not None and a is not None:
                    odds.append(build_match_record(home_team, away_team, "Fortebet", h, d, a, "Football"))
            except Exception:
                continue
    except Exception as e:
        print(f"Fortebet error: {e}")
    return odds


def scrape_sportybet():
    odds = []
    try:
        req = urllib.request.Request(SPORTYBET_API, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, list):
            for event in data:
                try:
                    home = event.get("home_team", "")
                    away = event.get("away_team", "")
                    h = clean_odd(event.get("home"))
                    d = clean_odd(event.get("draw"))
                    a = clean_odd(event.get("away"))
                    if home and away and h is not None and a is not None:
                        odds.append(
                            build_match_record(
                                home,
                                away,
                                "SportyBet",
                                h,
                                d,
                                a,
                                event.get("sport", "Football"),
                                event.get("competition", ""),
                            )
                        )
                except Exception:
                    continue
    except Exception as e:
        print(f"SportyBet error: {e}")
    return odds


def scrape_1xbet():
    odds = []
    try:
        url = "https://1xbet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=135&getEmpty=true&virtualSports=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = json.loads(raw.decode("utf-8-sig"))
        for match in data.get("Value", []) if isinstance(data, dict) else []:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue
                h = d = a = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        h = c
                    elif t == "2":
                        a = c
                    elif t == "3":
                        d = c
                if h is not None and a is not None:
                    odds.append(build_match_record(home_team, away_team, "1xBet", h, d, a, "Football"))
            except Exception:
                continue
    except Exception as e:
        print(f"1xBet error: {e}")
    return odds


def scrape_22bet():
    odds = []
    try:
        url = "https://22bet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=151&getEmpty=true&virtualSports=true"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8-sig"))
        for match in data.get("Value", []) if isinstance(data, dict) else []:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue
                h = d = a = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        h = c
                    elif t == "2":
                        a = c
                    elif t == "3":
                        d = c
                if h is not None and a is not None:
                    odds.append(build_match_record(home_team, away_team, "22Bet", h, d, a, "Football"))
            except Exception:
                continue
    except Exception as e:
        print(f"22Bet error: {e}")
    return odds


def scrape_melbet():
    odds = []
    try:
        url = "https://melbet-046935.top/service-api/LineFeed/Get1x2_VZip?count=1000&lng=en&mode=4&country=191&partner=8&getEmpty=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-mobile-project-id": "0",
            "x-requested-with": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8-sig"))
        for match in data.get("Value", []) if isinstance(data, dict) else []:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team:
                    continue
                h = d = a = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        h = c
                    elif t == "2":
                        a = c
                    elif t == "3":
                        d = c
                if h is not None and a is not None:
                    odds.append(build_match_record(home_team, away_team, "Melbet", h, d, a, "Football"))
            except Exception:
                continue
    except Exception as e:
        print(f"Melbet error: {e}")
    return odds


def find_arbitrage(all_odds):
    opportunities = []
    sports_odds = {}
    for odd in all_odds:
        sports_odds.setdefault(odd.get("sport", "Football"), []).append(odd)

    for sport, sport_odds in sports_odds.items():
        exact_groups = {}
        for odd in sport_odds:
            exact_groups.setdefault(odd.get("match_key", ""), []).append(odd)

        merged_groups = {}
        processed = set()
        keys = list(exact_groups.keys())
        for i, key1 in enumerate(keys):
            if key1 in processed:
                continue
            group = list(exact_groups[key1])
            processed.add(key1)
            for key2 in keys[i + 1 :]:
                if key2 in processed:
                    continue
                if match_key_similarity(key1, key2):
                    group.extend(exact_groups[key2])
                    processed.add(key2)
            merged_groups[key1] = group

        for match_name, bookmakers in merged_groups.items():
            bookie_names = set(b["bookmaker"] for b in bookmakers)
            if len(bookie_names) < 2:
                continue

            bk_odds = {}
            for b in bookmakers:
                bk = b["bookmaker"]
                bk_odds.setdefault(bk, {"home": 0.0, "draw": 0.0, "away": 0.0})
                h = clean_odd(b.get("home"))
                d = clean_odd(b.get("draw"))
                a = clean_odd(b.get("away"))
                if h is not None and h > bk_odds[bk]["home"]:
                    bk_odds[bk]["home"] = h
                if d is not None and d > bk_odds[bk]["draw"]:
                    bk_odds[bk]["draw"] = d
                if a is not None and a > bk_odds[bk]["away"]:
                    bk_odds[bk]["away"] = a

            bk_list = list(bk_odds.keys())

            if sport in ["Football", "Rugby", "Futsal"]:
                best = None
                for bk_h in bk_list:
                    for bk_d in bk_list:
                        for bk_a in bk_list:
                            if len({bk_h, bk_d, bk_a}) < 3:
                                continue
                            h, d, a = bk_odds[bk_h]["home"], bk_odds[bk_d]["draw"], bk_odds[bk_a]["away"]
                            if not h or not d or not a:
                                continue
                            arb = (1 / h) + (1 / d) + (1 / a)
                            if arb < 1:
                                profit = round((1 - arb) * 100, 2)
                                if 1.0 <= profit <= 50.0 and (best is None or profit > best["profit_percent"]):
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
                                            {
                                                "bookmaker": bk_h,
                                                "outcome": "Home",
                                                "odd": h,
                                                "stake": stake_h,
                                                "win": round(stake_h * h),
                                            },
                                            {
                                                "bookmaker": bk_d,
                                                "outcome": "Draw",
                                                "odd": d,
                                                "stake": stake_d,
                                                "win": round(stake_d * d),
                                            },
                                            {
                                                "bookmaker": bk_a,
                                                "outcome": "Away",
                                                "odd": a,
                                                "stake": stake_a,
                                                "win": round(stake_a * a),
                                            },
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
                        h, a = bk_odds[bk_h]["home"], bk_odds[bk_a]["away"]
                        if not h or not a:
                            continue
                        arb = (1 / h) + (1 / a)
                        if arb < 1:
                            profit = round((1 - arb) * 100, 2)
                            if 1.0 <= profit <= 50.0 and (best is None or profit > best["profit_percent"]):
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
                                        {
                                            "bookmaker": bk_h,
                                            "outcome": "Home",
                                            "odd": h,
                                            "stake": stake_h,
                                            "win": round(stake_h * h),
                                        },
                                        {
                                            "bookmaker": bk_a,
                                            "outcome": "Away",
                                            "odd": a,
                                            "stake": stake_a,
                                            "win": round(stake_a * a),
                                        },
                                    ],
                                }
                if best:
                    opportunities.append(best)

    return sorted(opportunities, key=lambda x: x["profit_percent"], reverse=True)


def opportunity_id(o):
    bets = o.get("bets", []) or []
    bet_key = "|".join(f"{b.get('bookmaker')}:{b.get('outcome')}:{b.get('odd')}" for b in bets)
    return f"{o.get('match','')}|{o.get('type','')}|{bet_key}"


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def refresh_opportunities(current_opps):
    """
    Keep previous opportunities, mark still-present ones as valid,
    disappeared ones as invalid, and detect when bookmakers change.
    """
    previous = load_history()
    next_history = {}
    current_ids = set()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    def bookmaker_signature(opp):
        bets = opp.get("bets", []) or []
        pairs = sorted((b.get("bookmaker"), b.get("outcome")) for b in bets)
        return pairs

    for opp in current_opps:
        base_key = f"{opp.get('match','')}|{opp.get('type','')}"
        bets_sig = bookmaker_signature(opp)

        prev_candidate_id = None
        prev_candidate = None
        for oid, old in previous.items():
            if f"{old.get('match','')}|{old.get('type','')}" == base_key:
                prev_candidate_id = oid
                prev_candidate = old
                break

        if prev_candidate is not None:
            oid = opportunity_id(opp)
            current_ids.add(oid)

            old_sig = bookmaker_signature(prev_candidate)

            opp["status"] = "valid"
            opp["prev_status"] = prev_candidate.get("status", "valid")
            opp["checked_at"] = now_str

            if bets_sig == old_sig:
                opp["changed"] = False
                opp["note"] = "Still valid (same bookmakers)"
            else:
                opp["changed"] = True
                opp["note"] = "Valid (bookmakers changed)"

            next_history[oid] = opp
        else:
            oid = opportunity_id(opp)
            current_ids.add(oid)

            opp["status"] = "valid"
            opp["changed"] = True
            opp["prev_status"] = "new"
            opp["note"] = "New opportunity"
            opp["checked_at"] = now_str

            next_history[oid] = opp

    for oid, old in previous.items():
        if oid not in current_ids:
            old["status"] = "invalid"
            old["changed"] = False
            old["note"] = "No longer present"
            old["invalid_at"] = now_str
            next_history[oid] = old

    save_history(next_history)
    all_opps = list(next_history.values())
    return {
        "all_opportunities": all_opps,
        "valid_opportunities": [o for o in all_opps if o.get("status") == "valid"],
        "invalid_opportunities": [o for o in all_opps if o.get("status") == "invalid"],
    }


def main():
    all_odds = []
    scraped = []

    for name, func in [
        ("ChampionBet", scrape_championbet),
        ("Betika", scrape_betika),
        ("BetPawa", scrape_betpawa),
        ("Fortebet", scrape_fortebet),
        ("SportyBet", scrape_sportybet),
        ("AbaBet", scrape_ababet),
        ("1xBet", scrape_1xbet),
        ("22Bet", scrape_22bet),
        ("Melbet", scrape_melbet),
        # ("PMBet", scrape_pmbet),  # add later when ready
    ]:
        print(f"Scraping {name}...")
        rows = func()
        all_odds.extend(rows)
        if rows:
            scraped.append(name)

    fresh_opps = find_arbitrage(all_odds)
    opps_result = refresh_opportunities(fresh_opps)
    output = {
        "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "total_matches": len(all_odds),
        "bookmakers_scraped": scraped,
        "raw_odds": all_odds,
        "opportunities": opps_result["all_opportunities"],
        "valid_opportunities": opps_result["valid_opportunities"],
        "invalid_opportunities": opps_result["invalid_opportunities"],
    }
    with open("odds.json", "w") as f:
        json.dump(output, f, indent=2)
    print(
        f"Done. Matches: {len(all_odds)}, "
        f"new run opportunities: {len(fresh_opps)}, "
        f"total kept in history: {len(opps_result['all_opportunities'])}"
    )


if __name__ == "__main__":
    main()



