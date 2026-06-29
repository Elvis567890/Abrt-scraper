import json
from datetime import datetime
from playwright.sync_api import sync_playwright
import re
import urllib.request
import urllib.parse
import requests
import time

SPORTYBET_API = 'https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple'

def normalize(name):
    name = name.lower().strip()
    name = re.sub(r'\b(fc|sc|cf|ac|united|city|sports|club|utd|football|soccer|women|men|u21|u23)\b', '', name)
    name = re.sub(r'[^a-z0-9 ]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def teams_match(name1, name2):
    n1 = normalize(name1)
    n2 = normalize(name2)
    if n1 == n2:
        return True
    if len(n1) > 3 and len(n2) > 3:
        if n1 in n2 or n2 in n1:
            return True
        w1 = n1.split()[0] if n1.split() else ''
        w2 = n2.split()[0] if n2.split() else ''
        if len(w1) > 4 and w1 == w2:
            return True
    return False

def match_key_similarity(key1, key2):
    parts1 = key1.split(' vs ')
    parts2 = key2.split(' vs ')
    if len(parts1) != 2 or len(parts2) != 2:
        return False
    return teams_match(parts1[0], parts2[0]) and teams_match(parts1[1], parts2[1])

def scrape_betpawa():
    odds = []
    seen_matches = set()
    urls = [
        'https://www.betpawa.ug/events?categoryId=2&marketId=1X2',
        'https://www.betpawa.ug/events/popular',
    ]
    skip = ['pm','am','Sat','Sun','Mon','Tue','Wed','Thu','Fri','Full Time','Half','1UP','2UP','1X2','Double','Both','Over','Under','Total','Score','Chance','Teams','Interval','minutes','First']
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-blink-features=AutomationControlled'])
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Linux; Android 12; Samsung Galaxy) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
                viewport={'width': 390, 'height': 844},
                locale='en-UG'
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
                            parts = [p.strip() for p in text.split('\n') if p.strip()]
                            teams = []
                            odd_values = []
                            competition = ''
                            for part in parts:
                                if re.match(r'^\d+\.\d+$', part):
                                    odd_values.append(float(part))
                                elif any(s in part for s in ['Football','Soccer','Netball','Tennis','Basketball']):
                                    competition = part
                                elif part in ['1','X','2','1X','X2','12']:
                                    continue
                                elif any(s in part for s in skip):
                                    continue
                                elif re.match(r'^\d+:\d+', part):
                                    continue
                                elif re.match(r'^\d+/\d+', part):
                                    continue
                                elif len(part) > 2:
                                    teams.append(part)
                            if len(teams) >= 2 and len(odd_values) >= 2:
                                match_key = f"{teams[0]}vs{teams[1]}".lower().replace(' ','')
                                if match_key not in seen_matches:
                                    seen_matches.add(match_key)
                                    page_odds += 1
                                    odds.append({
                                        'match': f"{teams[0]} vs {teams[1]}",
                                        'home_team': teams[0],
                                        'away_team': teams[1],
                                        'match_key': f"{normalize(teams[0])} vs {normalize(teams[1])}",
                                        'bookmaker': 'BetPawa',
                                        'competition': competition,
                                        'home': odd_values[0],
                                        'draw': odd_values[1] if len(odd_values) >= 3 else None,
                                        'away': odd_values[2] if len(odd_values) >= 3 else odd_values[1],
                                        'sport': 'Netball' if 'Netball' in competition else 'Football'
                                    })
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
        url = 'https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Referer': 'https://desktop.fortebet.ug/prematch/landing'
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        inner = data.get('data', {})
        events = inner.get('event', {})
        markets = inner.get('markets', {})
        competitors = inner.get('competitors', {})
        print(f"Fortebet: {len(events)} events, {len(markets)} markets, {len(competitors)} competitors")
        event_markets = {}
        for mid, market in markets.items():
            eid = str(market.get('eventId',''))
            if eid not in event_markets:
                event_markets[eid] = []
            event_markets[eid].append(market)
        football_count = 0
        for eid, event in events.items():
            try:
                comp_ids = event.get('competitors', [])
                if len(comp_ids) < 2:
                    continue
                home_team = competitors.get(str(comp_ids[0]), {}).get('name','')
                away_team = competitors.get(str(comp_ids[1]), {}).get('name','')
                if not home_team or not away_team:
                    continue
                mkt_list = event_markets.get(eid, [])
                h_odd = d_odd = a_odd = None
                for market in mkt_list:
                    if market.get('marketId') == 1:
                        mkt_odds = market.get('odds', {})
                        odd_list = []
                        for k, v in mkt_odds.items():
                            if isinstance(v, dict) and 'odds' in v:
                                odd_list.append((v.get('outcomeId', 0), float(v['odds'])))
                        odd_list.sort(key=lambda x: x[0])
                        if len(odd_list) >= 3:
                            h_odd = odd_list[0][1]
                            d_odd = odd_list[1][1]
                            a_odd = odd_list[2][1]
                        elif len(odd_list) == 2:
                            h_odd = odd_list[0][1]
                            a_odd = odd_list[1][1]
                            d_odd = None
                        break
                if h_odd and a_odd:
                    football_count += 1
                    sport = 'Netball' if d_odd is None else 'Football'
                    odds.append({
                        'match': f"{home_team} vs {away_team}",
                        'home_team': home_team,
                        'away_team': away_team,
                        'match_key': f"{normalize(home_team)} vs {normalize(away_team)}",
                        'bookmaker': 'Fortebet',
                        'competition': '',
                        'home': h_odd,
                        'draw': d_odd,
                        'away': a_odd,
                        'sport': sport
                    })
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
        req = urllib.request.Request(SPORTYBET_API, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, list):
            sport_counts = {}
            for event in data:
                try:
                    home = event.get('home_team','')
                    away = event.get('away_team','')
                    sport = event.get('sport','Football')
                    h_odd = float(event.get('home', 0) or 0)
                    d_odd = event.get('draw')
                    a_odd = float(event.get('away', 0) or 0)
                    if d_odd:
                        d_odd = float(d_odd)
                    if home and away and h_odd and a_odd:
                        sport_counts[sport] = sport_counts.get(sport, 0) + 1
                        odds.append({
                            'match': f"{home} vs {away}",
                            'home_team': home,
                            'away_team': away,
                            'match_key': f"{normalize(home)} vs {normalize(away)}",
                            'bookmaker': 'SportyBet',
                            'competition': '',
                            'home': h_odd,
                            'draw': d_odd,
                            'away': a_odd,
                            'sport': sport
                        })
                except:
                    continue
            print(f"SportyBet sports breakdown: {sport_counts}")
        print(f"SportyBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"SportyBet error: {e}")
    return odds

def scrape_1xbet():
    odds = []
    try:
        print("Fetching 1xBet Uganda...")
        url = "https://1xbet.ug/service-api/LineFeed/GetSportsShortZip?lng=en&country=191&partner=135&virtualSports=true&gr=640&groupChamps=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except:
                data = json.loads(raw.decode("utf-8-sig"))
        odds_data = data if isinstance(data, dict) else {}
        events = odds_data.get("Value", []) if isinstance(odds_data, dict) else []
        count = 0
        for evt in events:
            try:
                home_team = evt.get("O1") or evt.get("O1E") or evt.get("HomeTeam")
                away_team = evt.get("O2") or evt.get("O2E") or evt.get("AwayTeam")
                if not home_team or not away_team:
                    continue
                home_odd = None
                draw_odd = None
                away_odd = None
                sport = "Football"
                for mkt in evt.get("E", []):
                    market_name = str(mkt.get("NA", "")).lower()
                    selections = mkt.get("GC", [])
                    if market_name in ("1x2", "match result", "full time result", "winner"):
                        for sel in selections:
                            label = str(sel.get("T", "")).strip().lower()
                            price = sel.get("C")
                            if price is None:
                                continue
                            try:
                                price = float(price)
                            except:
                                continue
                            if label in ("1", "home"):
                                home_odd = price
                            elif label in ("x", "draw"):
                                draw_odd = price
                            elif label in ("2", "away"):
                                away_odd = price
                    elif len(selections) == 2:
                        for sel in selections:
                            label = str(sel.get("T", "")).strip().lower()
                            price = sel.get("C")
                            if price is None:
                                continue
                            try:
                                price = float(price)
                            except:
                                continue
                            if "home" in label or label == "1":
                                home_odd = price
                            elif "away" in label or label == "2":
                                away_odd = price
                    if home_odd and away_odd:
                        break
                if home_odd and away_odd:
                    count += 1
                    odds.append({
                        "match": f"{home_team} vs {away_team}",
                        "home_team": home_team,
                        "away_team": away_team,
                        "match_key": f"{normalize(home_team)} vs {normalize(away_team)}",
                        "bookmaker": "1xBet",
                        "home": home_odd,
                        "draw": draw_odd,
                        "away": away_odd,
                        "sport": sport
                    })
            except:
                continue
        print(f"1xBet: {count} matches extracted")
    except Exception as e:
        print(f"1xBet error: {e}")
    return odds

def find_arbitrage(all_odds):
    opportunities = []
    STAKE = 100000
    sports_odds = {}
    for odd in all_odds:
        sport = odd.get('sport', 'Football')
        if sport not in sports_odds:
            sports_odds[sport] = []
        sports_odds[sport].append(odd)
    for sport, sport_odds in sports_odds.items():
        exact_groups = {}
        for odd in sport_odds:
            key = odd.get('match_key', '')
            if key not in exact_groups:
                exact_groups[key] = []
            exact_groups[key].append(odd)
        merged_groups = {}
        processed_keys = set()
        all_keys = list(exact_groups.keys())
        for i, key1 in enumerate(all_keys):
            if key1 in processed_keys:
                continue
            group = list(exact_groups[key1])
            processed_keys.add(key1)
            for key2 in all_keys[i+1:]:
                if key2 in processed_keys:
                    continue
                if match_key_similarity(key1, key2):
                    group.extend(exact_groups[key2])
                    processed_keys.add(key2)
            merged_groups[key1] = group
        for match_name, bookmakers in merged_groups.items():
            bookie_names = set(b['bookmaker'] for b in bookmakers)
            if len(bookie_names) < 2:
                continue
            bk_odds = {}
            for b in bookmakers:
                bk = b['bookmaker']
                if bk not in bk_odds:
                    bk_odds[bk] = {'home': 0, 'draw': 0, 'away': 0}
                if b.get('home', 0) > bk_odds[bk]['home']:
                    bk_odds[bk]['home'] = b['home']
                if b.get('draw') and b['draw'] > bk_odds[bk]['draw']:
                    bk_odds[bk]['draw'] = b['draw']
                if b.get('away', 0) > bk_odds[bk]['away']:
                    bk_odds[bk]['away'] = b['away']
            bk_list = list(bk_odds.keys())
            if sport in ['Football', 'Rugby', 'Futsal']:
                best = None
                for bk_h in bk_list:
                    for bk_d in bk_list:
                        for bk_a in bk_list:
                            if bk_h == bk_d == bk_a:
                                continue
                            h = bk_odds[bk_h]['home']
                            d = bk_odds[bk_d]['draw']
                            a = bk_odds[bk_a]['away']
                            if not h or not d or not a:
                                continue
                            if not all(1.01 <= x <= 50 for x in [h, d, a]):
                                continue
                            arb = (1/h)+(1/d)+(1/a)
                            if arb < 1:
                                profit = round((1-arb)*100, 2)
                                if 0.1 <= profit <= 8.0:
                                    if best is None or profit > best['profit_percent']:
                                        stake_h = round(STAKE*(1/h)/arb)
                                        stake_d = round(STAKE*(1/d)/arb)
                                        stake_a = round(STAKE*(1/a)/arb)
                                        best = {
                                            'match': match_name,
                                            'sport': sport,
                                            'type': '3-way',
                                            'profit_percent': profit,
                                            'profit_ugx': round(STAKE*(1-arb)),
                                            'total_stake': STAKE,
                                            'arb_sum': round(arb, 4),
                                            'bets': [
                                                {'bookmaker': bk_h,'outcome':'Home','odd': h,'stake': stake_h,'win': round(stake_h*h)},
                                                {'bookmaker': bk_d,'outcome':'Draw','odd': d,'stake': stake_d,'win': round(stake_d*d)},
                                                {'bookmaker': bk_a,'outcome':'Away','odd': a,'stake': stake_a,'win': round(stake_a*a)}
                                            ]
                                        }
                if best:
                    opportunities.append(best)
            else:
                best = None
                for bk_h in bk_list:
                    for bk_a in bk_list:
                        if bk_h == bk_a:
                            continue
                        h = bk_odds[bk_h]['home']
                        a = bk_odds[bk_a]['away']
                        if not h or not a:
                            continue
                        if not all(1.01 <= x <= 50 for x in [h, a]):
                            continue
                        arb = (1/h)+(1/a)
                        if arb < 1:
                            profit = round((1-arb)*100, 2)
                            if 0.1 <= profit <= 8.0:
                                if best is None or profit > best['profit_percent']:
                                    stake_h = round(STAKE*(1/h)/arb)
                                    stake_a = round(STAKE*(1/a)/arb)
                                    best = {
                                        'match': match_name,
                                        'sport': sport,
                                        'type': '2-way',
                                        'profit_percent': profit,
                                        'profit_ugx': round(STAKE*(1-arb)),
                                        'total_stake': STAKE,
                                        'arb_sum': round(arb, 4),
                                        'bets': [
                                            {'bookmaker': bk_h,'outcome':'Home','odd': h,'stake': stake_h,'win': round(stake_h*h)},
                                            {'bookmaker': bk_a,'outcome':'Away','odd': a,'stake': stake_a,'win': round(stake_a*a)}
                                        ]
                                    }
                if best:
                    opportunities.append(best)
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

def main():
    print(f"Scraper started: {datetime.utcnow()}")
    all_odds = []
    scraped = []
    print("Scraping BetPawa...")
    bp = scrape_betpawa()
    all_odds.extend(bp)
    if bp: scraped.append('BetPawa')
    print("Scraping Fortebet...")
    fb = scrape_fortebet()
    all_odds.extend(fb)
    if fb: scraped.append('Fortebet')
    print("Scraping SportyBet...")
    sb = scrape_sportybet()
    all_odds.extend(sb)
    if sb: scraped.append('SportyBet')
    print("Scraping 1xBet...")
    x1 = scrape_1xbet()
    all_odds.extend(x1)
    if x1: scraped.append('1xBet')
    opportunities = find_arbitrage(all_odds)
    print(f"Found {len(opportunities)} arbitrage opportunities")
    for o in opportunities[:5]:
        print(f"  [{o['sport']}] {o['match']}")
        print(f"  {o['type']} | Profit: {o['profit_percent']}% | ARB: {o['arb_sum']} | UGX: {o['profit_ugx']:,}")
        for b in o['bets']:
            print(f"    {b['bookmaker']}: {b['outcome']} @ {b['odd']} → UGX {b['stake']:,} → win UGX {b['win']:,}")
        print()
    output = {
        'last_updated': datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
        'total_matches': len(all_odds),
        'bookmakers_scraped': scraped,
        'opportunities': opportunities,
        'raw_odds': all_odds
    }
    with open('odds.json', 'w') as f:
        json.dump(output, f, indent=2)
    print(f"Done! {len(all_odds)} matches saved")

if __name__ == '__main__':
    main()
