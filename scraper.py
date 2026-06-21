import json
from datetime import datetime
from playwright.sync_api import sync_playwright
import re
import urllib.request

SPORTYBET_API = 'https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple'

def normalize(name):
    name = name.lower().strip()
    name = re.sub(r'\b(fc|sc|cf|ac|united|city|sports|club|utd)\b', '', name)
    name = re.sub(r'[^a-z0-9 ]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

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

    # Group by normalized match key
    matches = {}
    for odd in all_odds:
        key = odd.get('match_key', odd['match'].lower().strip())
        if key not in matches:
            matches[key] = []
        matches[key].append(odd)

    for match_name, bookmakers in matches.items():
        # Must have at least 2 different bookmakers
        bookie_names = set(b['bookmaker'] for b in bookmakers)
        if len(bookie_names) < 2:
            continue

        # For each outcome, get ALL odds from ALL bookmakers
        # Then find best odd for each outcome from DIFFERENT bookmakers

        # Get all home odds per bookmaker
        home_odds = {}
        draw_odds = {}
        away_odds = {}
        for b in bookmakers:
            bk = b['bookmaker']
            if b.get('home') and (bk not in home_odds or b['home'] > home_odds[bk]['odd']):
                home_odds[bk] = {'odd': b['home'], 'bookmaker': bk}
            if b.get('draw') and (bk not in draw_odds or b['draw'] > draw_odds[bk]['odd']):
                draw_odds[bk] = {'odd': b['draw'], 'bookmaker': bk}
            if b.get('away') and (bk not in away_odds or b['away'] > away_odds[bk]['odd']):
                away_odds[bk] = {'odd': b['away'], 'bookmaker': bk}

        # Try all combinations of different bookmakers
        # 2-way: Home vs Away from different bookmakers
        for bk_h, h_data in home_odds.items():
            for bk_a, a_data in away_odds.items():
                if bk_h == bk_a:
                    continue  # Must be different bookmakers
                h = h_data['odd']
                a = a_data['odd']
                arb2 = (1/h) + (1/a)
                if arb2 < 1:
                    profit = round((1-arb2)*100, 2)
                    stake_h = round(STAKE*(1/h)/arb2)
                    stake_a = round(STAKE*(1/a)/arb2)
                    profit_ugx = round(STAKE - (stake_h + stake_a))
                    opportunities.append({
                        'match': match_name,
                        'type': '2-way',
                        'profit_percent': profit,
                        'profit_ugx': profit_ugx,
                        'total_stake': STAKE,
                        'bets': [
                            {'bookmaker': bk_h,'outcome':'Home','odd': h,'stake': stake_h},
                            {'bookmaker': bk_a,'outcome':'Away','odd': a,'stake': stake_a}
                        ]
                    })

        # 3-way: Home, Draw, Away from at least 2 different bookmakers
        for bk_h, h_data in home_odds.items():
            for bk_d, d_data in draw_odds.items():
                for bk_a, a_data in away_odds.items():
                    # At least 2 must be different
                    books = set([bk_h, bk_d, bk_a])
                    if len(books) < 2:
                        continue
                    # All 3 same bookmaker not allowed
                    if bk_h == bk_d == bk_a:
                        continue
                    h = h_data['odd']
                    d = d_data['odd']
                    a = a_data['odd']
                    arb3 = (1/h) + (1/d) + (1/a)
                    if arb3 < 1:
                        profit = round((1-arb3)*100, 2)
                        stake_h = round(STAKE*(1/h)/arb3)
                        stake_d = round(STAKE*(1/d)/arb3)
                        stake_a = round(STAKE*(1/a)/arb3)
                        profit_ugx = round(STAKE - (stake_h + stake_d + stake_a))
                        opportunities.append({
                            'match': match_name,
                            'type': '3-way',
                            'profit_percent': profit,
                            'profit_ugx': profit_ugx,
                            'total_stake': STAKE,
                            'bets': [
                                {'bookmaker': bk_h,'outcome':'Home','odd': h,'stake': stake_h},
                                {'bookmaker': bk_d,'outcome':'Draw','odd': d,'stake': stake_d},
                                {'bookmaker': bk_a,'outcome':'Away','odd': a,'stake': stake_a}
                            ]
                        })

    # Remove duplicates and sort by profit
    seen = set()
    unique = []
    for o in opportunities:
        key = f"{o['match']}_{o['type']}_{o['profit_percent']}"
        if key not in seen:
            seen.add(key)
            unique.append(o)

    return sorted(unique, key=lambda x: x['profit_percent'], reverse=True)

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
        print(f"  {o['match']} - {o['profit_percent']}% profit - {o['type']}")
        for b in o['bets']:
            print(f"    {b['bookmaker']}: {b['outcome']} @ {b['odd']} = UGX {b['stake']:,}")
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
