import json
from datetime import datetime
from playwright.sync_api import sync_playwright
import re
import urllib.request
from itertools import permutations

SPORTYBET_API = 'https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple'

def normalize(name):
    name = name.lower().strip()
    # Remove common suffixes
    name = re.sub(r'\b(fc|sc|cf|ac|united|city|sports|club|utd|football|soccer|women|men|u21|u23)\b', '', name)
    # Remove punctuation
    name = re.sub(r'[^a-z0-9 ]', '', name)
    # Remove extra spaces
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def teams_match(name1, name2):
    """Check if two team names refer to the same team"""
    n1 = normalize(name1)
    n2 = normalize(name2)
    if n1 == n2:
        return True
    # Check if one contains the other (e.g. "man utd" vs "manchester united")
    if len(n1) > 3 and len(n2) > 3:
        if n1 in n2 or n2 in n1:
            return True
        # Check first word match
        w1 = n1.split()[0] if n1.split() else ''
        w2 = n2.split()[0] if n2.split() else ''
        if len(w1) > 4 and w1 == w2:
            return True
    return False

def match_key_similarity(key1, key2):
    """Check if two match keys refer to same match"""
    parts1 = key1.split(' vs ')
    parts2 = key2.split(' vs ')
    if len(parts1) != 2 or len(parts2) != 2:
        return False
    home_match = teams_match(parts1[0], parts2[0])
    away_match = teams_match(parts1[1], parts2[1])
    return home_match and away_match

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
                                elif any(s in part for s in ['Football','Soccer']):
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
                            if len(teams) >= 2 and len(odd_values) >= 3:
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
                                        'draw': odd_values[1],
                                        'away': odd_values[2],
                                        'sport': 'Football'
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
                        break
                if h_odd and a_odd:
                    football_count += 1
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
                        'sport': 'Football'
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
            for event in data:
                try:
                    home = event.get('home_team','')
                    away = event.get('away_team','')
                    h_odd = float(event.get('home', 0))
                    d_odd = float(event.get('draw', 0))
                    a_odd = float(event.get('away', 0))
                    if home and away and h_odd and a_odd:
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
                            'sport': 'Football'
                        })
                except:
                    continue
        print(f"SportyBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"SportyBet error: {e}")
    return odds

def find_arbitrage(all_odds):
    opportunities = []
    STAKE = 100000

    # Group matches using fuzzy matching
    # First group by exact match_key
    exact_groups = {}
    for odd in all_odds:
        key = odd.get('match_key', '')
        if key not in exact_groups:
            exact_groups[key] = []
        exact_groups[key].append(odd)

    # Merge similar match keys (fuzzy matching)
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
        # Must have at least 2 different bookmakers
        bookie_names = set(b['bookmaker'] for b in bookmakers)
        if len(bookie_names) < 2:
            continue

        # Get best odd per outcome per bookmaker
        bk_odds = {}
        for b in bookmakers:
            bk = b['bookmaker']
            if bk not in bk_odds:
                bk_odds[bk] = {'home': 0, 'draw': 0, 'away': 0}
            if b.get('home', 0) > bk_odds[bk]['home']:
                bk_odds[bk]['home'] = b['home']
            if b.get('draw', 0) > bk_odds[bk]['draw']:
                bk_odds[bk]['draw'] = b['draw']
            if b.get('away', 0) > bk_odds[bk]['away']:
                bk_odds[bk]['away'] = b['away']

        bk_list = list(bk_odds.keys())

        # ============================================
        # 3-WAY ARBITRAGE
        # Home from BK_A, Draw from BK_B, Away from BK_C
        # At least 2 must be different bookmakers
        # ============================================
        best_3way = None
        for bk_h in bk_list:
            for bk_d in bk_list:
                for bk_a in bk_list:
                    # Must have at least 2 different bookmakers
                    books_used = set([bk_h, bk_d, bk_a])
                    if len(books_used) < 2:
                        continue
                    h = bk_odds[bk_h]['home']
                    d = bk_odds[bk_d]['draw']
                    a = bk_odds[bk_a]['away']
                    if not h or not d or not a:
                        continue
                    # Only accept realistic odds (between 1.01 and 50)
                    if not all(1.01 <= x <= 50 for x in [h, d, a]):
                        continue
                    arb = (1/h) + (1/d) + (1/a)
                    if arb < 1:
                        profit = round((1-arb)*100, 2)
                        # Only accept realistic profit (0.1% to 8%)
                        if 0.1 <= profit <= 8.0:
                            stake_h = round(STAKE*(1/h)/arb)
                            stake_d = round(STAKE*(1/d)/arb)
                            stake_a = round(STAKE*(1/a)/arb)
                            profit_ugx = round(STAKE*(1-arb))
                            if best_3way is None or profit > best_3way['profit_percent']:
                                best_3way = {
                                    'match': match_name,
                                    'type': '3-way',
                                    'profit_percent': profit,
                                    'profit_ugx': profit_ugx,
                                    'total_stake': STAKE,
                                    'arb_sum': round(arb, 4),
                                    'bets': [
                                        {'bookmaker': bk_h,'outcome':'Home','odd': h,'stake': stake_h,'win': round(stake_h*h)},
                                        {'bookmaker': bk_d,'outcome':'Draw','odd': d,'stake': stake_d,'win': round(stake_d*d)},
                                        {'bookmaker': bk_a,'outcome':'Away','odd': a,'stake': stake_a,'win': round(stake_a*a)}
                                    ]
                                }
        if best_3way:
            opportunities.append(best_3way)

        # ============================================
        # 2-WAY ARBITRAGE
        # Home from BK_A, Away from BK_B (different bookmakers)
        # ============================================
        best_2way = None
        for bk_h in bk_list:
            for bk_a in bk_list:
                if bk_h == bk_a:
                    continue  # Must be different bookmakers
                h = bk_odds[bk_h]['home']
                a = bk_odds[bk_a]['away']
                if not h or not a:
                    continue
                if not all(1.01 <= x <= 50 for x in [h, a]):
                    continue
                arb = (1/h) + (1/a)
                if arb < 1:
                    profit = round((1-arb)*100, 2)
                    if 0.1 <= profit <= 8.0:
                        stake_h = round(STAKE*(1/h)/arb)
                        stake_a = round(STAKE*(1/a)/arb)
                        profit_ugx = round(STAKE*(1-arb))
                        if best_2way is None or profit > best_2way['profit_percent']:
                            best_2way = {
                                'match': match_name,
                                'type': '2-way',
                                'profit_percent': profit,
                                'profit_ugx': profit_ugx,
                                'total_stake': STAKE,
                                'arb_sum': round(arb, 4),
                                'bets': [
                                    {'bookmaker': bk_h,'outcome':'Home','odd': h,'stake': stake_h,'win': round(stake_h*h)},
                                    {'bookmaker': bk_a,'outcome':'Away','odd': a,'stake': stake_a,'win': round(stake_a*a)}
                                ]
                            }
        if best_2way:
            opportunities.append(best_2way)

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
    opportunities = find_arbitrage(all_odds)
    print(f"Found {len(opportunities)} arbitrage opportunities")
    for o in opportunities[:5]:
        print(f"  MATCH: {o['match']}")
        print(f"  TYPE: {o['type']} | PROFIT: {o['profit_percent']}% | ARB SUM: {o['arb_sum']}")
        for b in o['bets']:
            print(f"    {b['bookmaker']}: {b['outcome']} @ {b['odd']} → stake UGX {b['stake']:,} → win UGX {b['win']:,}")
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
