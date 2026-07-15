import json
import logging
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# =============================================================================
# CONFIGURATION – tune these to your needs
# =============================================================================
STAKE = 100_000  # total investment per arb (UGX)
MIN_PROFIT_PERCENT = 0.5
MAX_PROFIT_PERCENT = 20.0
MAX_ODD_AGE = 30       # seconds – ignore odds older than this
SCAN_INTERVAL = 15     # seconds between full scans
STAKE_ROUND_TO = 100   # round individual stakes to nearest this value

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("arbitrage.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# =============================================================================
# HELPERS
# =============================================================================
def safe_float(v, default=None):
    """Safely convert to float, returning default on failure."""
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default

def clean_odd(v, min_odd=1.01, max_odd=50.0):
    """Return a valid odd or None."""
    odd = safe_float(v)
    if odd is None:
        return None
    return odd if min_odd <= odd <= max_odd else None

def normalize_team(name):
    """Light normalization: lowercase, remove punctuation, collapse spaces."""
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def teams_match(n1, n2, threshold=0.85):
    """Return True if team names are likely the same."""
    n1, n2 = normalize_team(n1), normalize_team(n2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    # use difflib for fuzzy matching
    return SequenceMatcher(None, n1, n2).ratio() >= threshold

def match_key_similarity(key1, key2, league1="", league2=""):
    """Check if two match keys (home vs away) describe the same fixture."""
    parts1, parts2 = key1.split(" vs "), key2.split(" vs ")
    if len(parts1) != 2 or len(parts2) != 2:
        return False
    home_match = teams_match(parts1[0], parts2[0])
    away_match = teams_match(parts1[1], parts2[1])
    if not (home_match and away_match):
        return False
    # Extra security: if leagues are available, they must be similar
    if league1 and league2:
        return teams_match(league1, league2, threshold=0.8)
    return True

def now_ts():
    """UTC timestamp in seconds."""
    return datetime.now(timezone.utc).timestamp()

def build_match_record(home_team, away_team, bookmaker, home, draw, away,
                       sport="Football", competition="", league=""):
    return {
        "match": f"{home_team} vs {away_team}",
        "home_team": home_team,
        "away_team": away_team,
        "match_key": f"{normalize_team(home_team)} vs {normalize_team(away_team)}",
        "bookmaker": bookmaker,
        "competition": competition,
        "league": league or competition,   # used for matching
        "home": home,
        "draw": draw,
        "away": away,
        "sport": sport,
        "fetched_at": now_ts(),
    }

def round_stakes(stakes_raw, total):
    """Round individual stakes to STAKE_ROUND_TO while preserving total."""
    rounded = [round(s / STAKE_ROUND_TO) * STAKE_ROUND_TO for s in stakes_raw]
    diff = total - sum(rounded)
    # distribute rounding error across bets
    if diff != 0:
        for i in range(abs(diff) // STAKE_ROUND_TO):
            idx = i % len(rounded)
            rounded[idx] += STAKE_ROUND_TO * (1 if diff > 0 else -1)
    return rounded

# =============================================================================
# BOOKMAKER SCRAPERS (unchanged logic, but wrapped for concurrency)
# =============================================================================

def scrape_championbet():
    odds = []
    try:
        logger.info("Scraping ChampionBet...")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 ...",
            "X-INSTANA-T": "2fbd167006ebd264",
            "X-INSTANA-S": "2fbd167006ebd264",
            "X-INSTANA-L": "1,correlationType=web;correlationId=2fbd167006ebd264",
        }
        req = urllib.request.Request(
            "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en",
            headers=headers
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            top_data = json.loads(resp.read().decode())
        matches = top_data.get("esMatches", []) or []
        for m in matches:
            if "Soccer" not in str(m.get("sportToken", "")):
                continue
            match_id = m.get("id")
            if not match_id:
                continue
            home_team = m.get("home") or ""
            away_team = m.get("away") or ""
            if not home_team or not away_team:
                continue
            # fetch details
            detail_url = f"https://www.championbet.ug/restapi/offer/en/match/{match_id}?annex=13&mobileVersion=2.47.4.3&locale=en"
            try:
                det_req = urllib.request.Request(detail_url, headers=headers)
                with urllib.request.urlopen(det_req, timeout=10) as dr:
                    detail = json.loads(dr.read().decode())
                bet_map = detail.get("betMap", {})
                # extract 1x2 using betMap logic (tt 1/4/7 etc.)
                home_odd = draw_odd = away_odd = None
                def pick_odd(keys):
                    for k in keys:
                        market = bet_map.get(str(k), {})
                        for _, item in market.items():
                            if isinstance(item, dict):
                                o = clean_odd(item.get("ov"))
                                if o is not None:
                                    return o
                    return None
                home_odd = pick_odd([1,4,7])
                draw_odd = pick_odd([2,5,8])
                away_odd = pick_odd([3,6,9])
                if home_odd and away_odd:
                    odds.append(build_match_record(
                        home_team, away_team, "ChampionBet",
                        home_odd, draw_odd, away_odd,
                        competition=m.get("leagueName","")
                    ))
            except Exception as e:
                logger.debug(f"ChampionBet detail failed for {match_id}: {e}")
    except Exception as e:
        logger.error(f"ChampionBet scraper: {e}")
    return odds

def scrape_ababet():
    odds = []
    try:
        logger.info("Scraping AbaBet...")
        r = requests.get("https://www.ababet.ug/soccer/match_result?mobile=1",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find("tr").find_all(["th","td"])]
            if "Home" not in headers or "Away" not in headers:
                continue
            for row in table.find_all("tr")[1:]:
                cols = [td.get_text(strip=True) for td in row.find_all(["td","th"])]
                if len(cols) < 5:
                    continue
                row_dict = dict(zip(headers, cols))
                home = row_dict.get("Home")
                away = row_dict.get("Away")
                if not home or not away or home == "-":
                    continue
                odds.append(build_match_record(
                    home, away, "AbaBet",
                    clean_odd(row_dict.get("1")),
                    clean_odd(row_dict.get("X")),
                    clean_odd(row_dict.get("2")),
                    competition=row_dict.get("League","")
                ))
    except Exception as e:
        logger.error(f"AbaBet scraper: {e}")
    return odds

def scrape_betpawa():
    odds = []
    seen = set()
    urls = [
        "https://www.betpawa.ug/events?categoryId=2&marketId=1X2",
        "https://www.betpawa.ug/events/popular",
    ]
    skip_words = ["pm","am","Sat","Sun","Mon","Tue","Wed","Thu","Fri",
                  "Full Time","Half","1UP","2UP","1X2","Double","Both",
                  "Over","Under","Total","Score","Chance","Teams","Interval","minutes","First"]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(user_agent="Mozilla/5.0 ...", viewport={"width":390,"height":844})
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            for url in urls:
                page.goto(url, timeout=30000)
                page.wait_for_timeout(4000)
                links = page.query_selector_all('a[href*="/event/"], a[href*="/match/"]')
                for link in links[:80]:
                    text = link.inner_text()
                    parts = [p.strip() for p in text.split("\n") if p.strip()]
                    teams, vals, comp = [], [], ""
                    for part in parts:
                        if re.match(r"^\d+\.\d+$", part):
                            vals.append(float(part))
                        elif any(s in part for s in ["Football","Soccer","Netball"]):
                            comp = part
                        elif part in ["1","X","2","1X","X2","12"] or any(w in part for w in skip_words):
                            continue
                        elif re.match(r"^\d+:\d+", part) or re.match(r"^\d+/\d+", part):
                            continue
                        elif len(part) > 2:
                            teams.append(part)
                    if len(teams) >= 2 and len(vals) >= 2:
                        key = f"{teams[0]}vs{teams[1]}".lower().replace(" ","")
                        if key not in seen:
                            seen.add(key)
                            odds.append(build_match_record(
                                teams[0], teams[1], "BetPawa",
                                vals[0],
                                vals[1] if len(vals)>=3 else None,
                                vals[2] if len(vals)>=3 else vals[1],
                                sport="Netball" if "Netball" in comp else "Football",
                                competition=comp
                            ))
            browser.close()
    except Exception as e:
        logger.error(f"BetPawa scraper: {e}")
    return odds

def scrape_fortebet():
    odds = []
    try:
        logger.info("Scraping Fortebet...")
        req = urllib.request.Request(
            "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en",
            headers={"User-Agent":"Mozilla/5.0","Accept":"application/json",
                     "Referer":"https://desktop.fortebet.ug/prematch/landing"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        inner = data.get("data", {})
        events = inner.get("event", {})
        markets = inner.get("markets", {})
        competitors = inner.get("competitors", {})
        # group markets by event
        ev_mkts = {}
        for mk_id, mk in markets.items():
            eid = str(mk.get("eventId"))
            ev_mkts.setdefault(eid, []).append(mk)
        for eid, ev in events.items():
            comp_ids = ev.get("competitors", [])
            if len(comp_ids) < 2:
                continue
            home = competitors.get(str(comp_ids[0]), {}).get("name","")
            away = competitors.get(str(comp_ids[1]), {}).get("name","")
            if not home or not away:
                continue
            h_odd = d_odd = a_odd = None
            for mk in ev_mkts.get(eid, []):
                if mk.get("marketId") == 1:
                    olist = []
                    for oid, odata in mk.get("odds", {}).items():
                        if isinstance(odata, dict):
                            o = clean_odd(odata.get("odds"))
                            if o is not None:
                                olist.append((odata.get("outcomeId",0), o))
                    olist.sort(key=lambda x: x[0])
                    if len(olist) >= 3:
                        h_odd, d_odd, a_odd = olist[0][1], olist[1][1], olist[2][1]
                    elif len(olist) == 2:
                        h_odd, a_odd = olist[0][1], olist[1][1]
                    break
            if h_odd and a_odd:
                odds.append(build_match_record(
                    home, away, "Fortebet", h_odd, d_odd, a_odd,
                    sport="Football" if d_odd is not None else "Netball"
                ))
    except Exception as e:
        logger.error(f"Fortebet scraper: {e}")
    return odds

def scrape_sportybet():
    odds = []
    try:
        logger.info("Scraping SportyBet...")
        req = urllib.request.Request(
            "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple",
            headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        for ev in data if isinstance(data, list) else []:
            home = ev.get("home_team","")
            away = ev.get("away_team","")
            sport = ev.get("sport","Football")
            h = clean_odd(ev.get("home"))
            d = clean_odd(ev.get("draw"))
            a = clean_odd(ev.get("away"))
            if home and away and h and a:
                odds.append(build_match_record(home, away, "SportyBet", h, d, a, sport=sport))
    except Exception as e:
        logger.error(f"SportyBet scraper: {e}")
    return odds

def scrape_betika():
    odds = []
    try:
        logger.info("Scraping Betika...")
        url = "https://api-ug.betika.com/v1/uo/matches?page=1&limit=200&tab=&sub_type_id=1&sport_id=3&sort_id=1&period_id=-1&esports=false"
        req = urllib.request.Request(url, headers={"Accept":"application/json","User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        for m in data.get("data", []):
            home = m.get("home_team","")
            away = m.get("away_team","")
            if not home or not away:
                continue
            h = d = a = None
            for mk in m.get("odds", []) or m.get("sub_types", []):
                if str(mk.get("sub_type_id")) != "1":
                    continue
                for sel in mk.get("odds", []):
                    outcome = (sel.get("odd_type") or sel.get("name") or "").strip()
                    price = clean_odd(sel.get("value") or sel.get("odd_value"))
                    if price is None:
                        continue
                    if outcome in ("1","Home"):
                        h = price
                    elif outcome in ("X","Draw"):
                        d = price
                    elif outcome in ("2","Away"):
                        a = price
            if h and a:
                odds.append(build_match_record(
                    home, away, "Betika", h, d, a,
                    competition=m.get("competition_name","")
                ))
    except Exception as e:
        logger.error(f"Betika scraper: {e}")
    return odds

def scrape_1xbet():
    odds = []
    try:
        logger.info("Scraping 1xBet...")
        url = "https://1xbet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=135&getEmpty=true&virtualSports=true"
        headers = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            data = json.loads(r.read().decode())
        for m in data.get("Value", []):
            home = m.get("O1","")
            away = m.get("O2","")
            if not home or not away:
                continue
            h = d = a = None
            for e in m.get("E", []):
                t = str(e.get("T","")).strip()
                c = clean_odd(e.get("C"))
                if c is None:
                    continue
                if t == "1": h = c
                elif t == "2": a = c
                elif t == "3": d = c
            if h and a:
                odds.append(build_match_record(home, away, "1xBet", h, d, a))
    except Exception as e:
        logger.error(f"1xBet scraper: {e}")
    return odds

def scrape_22bet():
    odds = []
    try:
        logger.info("Scraping 22Bet...")
        url = "https://22bet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=151&getEmpty=true&virtualSports=true"
        headers = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            data = json.loads(r.read().decode())
        for m in data.get("Value", []):
            home = m.get("O1","")
            away = m.get("O2","")
            if not home or not away:
                continue
            h = d = a = None
            for e in m.get("E", []):
                t = str(e.get("T","")).strip()
                c = clean_odd(e.get("C"))
                if c is None:
                    continue
                if t == "1": h = c
                elif t == "2": a = c
                elif t == "3": d = c
            if h and a:
                odds.append(build_match_record(home, away, "22Bet", h, d, a))
    except Exception as e:
        logger.error(f"22Bet scraper: {e}")
    return odds

def scrape_melbet():
    odds = []
    try:
        logger.info("Scraping Melbet...")
        url = "https://melbet-046935.top/service-api/LineFeed/Get1x2_VZip?count=1000&lng=en&mode=4&country=191&partner=8&getEmpty=true"
        headers = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            data = json.loads(r.read().decode())
        for m in data.get("Value", []):
            home = m.get("O1","")
            away = m.get("O2","")
            if not home or not away:
                continue
            h = d = a = None
            for e in m.get("E", []):
                t = str(e.get("T","")).strip()
                c = clean_odd(e.get("C"))
                if c is None:
                    continue
                if t == "1": h = c
                elif t == "2": a = c
                elif t == "3": d = c
            if h and a:
                odds.append(build_match_record(home, away, "Melbet", h, d, a))
    except Exception as e:
        logger.error(f"Melbet scraper: {e}")
    return odds

def scrape_gsb():
    odds = []
    try:
        logger.info("Scraping GSB...")
        params = {
            "timestamp": str(int(time.time()*1000)),
            "betTypeIds": "-1",
            "sportTypeIds": "31",
            "statusId": "0",
        }
        url = "https://gsb.ug/services/evapi/event/GetEvents?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "BrandId":"112","ChannelId":"4","Language":"en-US","Terminal":"gsb.ug",
            "User-Agent":"Mozilla/5.0"
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        for ev in data.get("data", []):
            home = ev.get("h","")
            away = ev.get("a","")
            if not home or not away:
                continue
            h = d = a = None
            for bt in ev.get("bts", []):
                if bt.get("n","") != "FT 1X2":
                    continue
                for o in bt.get("odds", []):
                    sel = o.get("n","").strip()
                    p = clean_odd(o.get("p"))
                    if p is None: continue
                    if sel == "1": h = p
                    elif sel == "X": d = p
                    elif sel == "2": a = p
            if h and a:
                odds.append(build_match_record(
                    home, away, "GSB", h, d, a,
                    competition=ev.get("ln","")
                ))
    except Exception as e:
        logger.error(f"GSB scraper: {e}")
    return odds

# List of all scraper functions
SCRAPERS = [
    scrape_championbet,
    scrape_ababet,
    scrape_betpawa,
    scrape_fortebet,
    scrape_sportybet,
    scrape_betika,
    scrape_1xbet,
    scrape_22bet,
    scrape_melbet,
    scrape_gsb,
]

# =============================================================================
# ARBITRAGE DETECTION ENGINE
# =============================================================================
def find_arbitrage(all_odds):
    """Find arbitrage opportunities from list of match odds."""
    now = now_ts()
    # filter out stale odds
    fresh_odds = [o for o in all_odds if (now - o.get("fetched_at", 0)) <= MAX_ODD_AGE]
    logger.info(f"Total fresh odds: {len(fresh_odds)} (dropped {len(all_odds)-len(fresh_odds)} stale)")

    # Group by sport
    by_sport = {}
    for o in fresh_odds:
        by_sport.setdefault(o.get("sport", "Football"), []).append(o)

    opportunities = []
    for sport, odds_list in by_sport.items():
        # Group by exact match_key first
        exact_groups = {}
        for odd in odds_list:
            exact_groups.setdefault(odd["match_key"], []).append(odd)

        # Merge similar keys
        merged_groups = {}
        processed = set()
        keys = list(exact_groups.keys())
        for i, k1 in enumerate(keys):
            if k1 in processed:
                continue
            group = list(exact_groups[k1])
            processed.add(k1)
            for k2 in keys[i+1:]:
                if k2 in processed:
                    continue
                # get league from first odd of each group
                lg1 = exact_groups[k1][0].get("league","")
                lg2 = exact_groups[k2][0].get("league","")
                if match_key_similarity(k1, k2, lg1, lg2):
                    group.extend(exact_groups[k2])
                    processed.add(k2)
            merged_groups[k1] = group

        for match_name, records in merged_groups.items():
            # collect best odds per bookmaker
            bk_odds = {}
            for rec in records:
                bk = rec["bookmaker"]
                if bk not in bk_odds:
                    bk_odds[bk] = {"home": 0.0, "draw": 0.0, "away": 0.0}
                for key in ["home","draw","away"]:
                    val = clean_odd(rec.get(key))
                    if val and val > bk_odds[bk][key]:
                        bk_odds[bk][key] = val

            bookies = list(bk_odds.keys())
            if len(bookies) < 2:
                continue

            # Determine if 3-way sport
            is_3way = sport in ["Football","Rugby","Futsal"]

            if is_3way:
                best = None
                # Try all possible triples of bookmakers (allow duplicates)
                for bk_h in bookies:
                    for bk_d in bookies:
                        for bk_a in bookies:
                            if bk_h == bk_d == bk_a:  # must use at least 2 distinct
                                continue
                            h = bk_odds[bk_h]["home"]
                            d = bk_odds[bk_d]["draw"]
                            a = bk_odds[bk_a]["away"]
                            if not h or not d or not a:
                                continue
                            arb = 1/h + 1/d + 1/a
                            if arb < 1:
                                profit = (1 - arb) * 100
                                if MIN_PROFIT_PERCENT <= profit <= MAX_PROFIT_PERCENT:
                                    if best is None or profit > best["profit_percent"]:
                                        raw_stakes = [
                                            STAKE * (1/h) / arb,
                                            STAKE * (1/d) / arb,
                                            STAKE * (1/a) / arb,
                                        ]
                                        stakes = round_stakes(raw_stakes, STAKE)
                                        best = {
                                            "match": match_name,
                                            "sport": sport,
                                            "type": "3-way",
                                            "profit_percent": round(profit,2),
                                            "profit_ugx": round(STAKE * (1 - arb)),
                                            "total_stake": STAKE,
                                            "bets": [
                                                {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stakes[0]},
                                                {"bookmaker": bk_d, "outcome": "Draw", "odd": d, "stake": stakes[1]},
                                                {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stakes[2]},
                                            ]
                                        }
                if best:
                    opportunities.append(best)
            else:
                # 2-way
                best = None
                for bk_h in bookies:
                    for bk_a in bookies:
                        if bk_h == bk_a:
                            continue
                        h = bk_odds[bk_h]["home"]
                        a = bk_odds[bk_a]["away"]
                        if not h or not a:
                            continue
                        arb = 1/h + 1/a
                        if arb < 1:
                            profit = (1 - arb) * 100
                            if MIN_PROFIT_PERCENT <= profit <= MAX_PROFIT_PERCENT:
                                if best is None or profit > best["profit_percent"]:
                                    raw_stakes = [STAKE*(1/h)/arb, STAKE*(1/a)/arb]
                                    stakes = round_stakes(raw_stakes, STAKE)
                                    best = {
                                        "match": match_name,
                                        "sport": sport,
                                        "type": "2-way",
                                        "profit_percent": round(profit,2),
                                        "profit_ugx": round(STAKE * (1 - arb)),
                                        "total_stake": STAKE,
                                        "bets": [
                                            {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stakes[0]},
                                            {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stakes[1]},
                                        ]
                                    }
                if best:
                    opportunities.append(best)

    opportunities.sort(key=lambda x: x["profit_percent"], reverse=True)
    return opportunities

# =============================================================================
# REPORTING
# =============================================================================
def generate_html(opportunities, last_updated):
    rows = []
    for opp in opportunities:
        bets_str = "; ".join(f"{b['bookmaker']} {b['outcome']} @ {b['odd']}" for b in opp["bets"])
        rows.append(
            f"<tr><td>{opp['match']}</td><td>{opp['sport']}</td><td>{opp['type']}</td>"
            f"<td>{opp['profit_percent']}%</td><td>{opp['profit_ugx']} UGX</td><td>{bets_str}</td></tr>"
        )
    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>Arbitrage Opportunities</title>
<style>body{{font-family:Arial;margin:20px}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:6px;font-size:14px}} th{{background:#f0f0f0}}</style>
</head><body><h1>Live Arbitrage Opportunities</h1><p>Last scan: {last_updated}</p>
<table><tr><th>Match</th><th>Sport</th><th>Type</th><th>Profit %</th><th>Profit UGX</th><th>Bets</th></tr>
{''.join(rows)}</table></body></html>"""
    with open("odds.html", "w", encoding="utf-8") as f:
        f.write(html)

def save_json(opportunities, last_updated):
    with open("opportunities.json", "w", encoding="utf-8") as f:
        json.dump({"last_updated": last_updated, "opportunities": opportunities}, f, indent=2)

# =============================================================================
# MAIN SCANNING LOOP
# =============================================================================
def run_scan():
    """One complete scan: fetch odds, find arbs, write reports."""
    all_odds = []
    # Run all scrapers concurrently
    with ThreadPoolExecutor(max_workers=len(SCRAPERS)) as executor:
        future_to_scraper = {executor.submit(scraper): scraper.__name__ for scraper in SCRAPERS}
        for future in as_completed(future_to_scraper):
            name = future_to_scraper[future]
            try:
                odds = future.result()
                all_odds.extend(odds)
                logger.info(f"{name}: {len(odds)} odds")
            except Exception as e:
                logger.error(f"{name} failed: {e}")

    logger.info(f"Total odds collected: {len(all_odds)}")
    arbs = find_arbitrage(all_odds)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.info(f"Found {len(arbs)} arbitrage opportunities")

    generate_html(arbs, now_str)
    save_json(arbs, now_str)
    return arbs

def main():
    logger.info("Arbitrage engine started. Press Ctrl+C to stop.")
    try:
        while True:
            run_scan()
            time.sleep(SCAN_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Engine stopped.")

if __name__ == "__main__":
    main()
