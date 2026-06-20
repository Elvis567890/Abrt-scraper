import json
from datetime import datetime
from playwright.sync_api import sync_playwright
import re
import urllib.request
import urllib.error

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
                                    odds.append({'match': f"{teams[0]} vs {teams[1]}",'home_team': teams[0],'away_team': teams[1],'bookmaker': 'BetPawa','competition': competition,'home': odd_values[0],'draw': odd_values[1],'away': odd_values[2],'sport': 'Football'})
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
                    odds.append({'match': f"{home_team} vs {away_team}",'home_team': home_team,'away_team': away_team,'bookmaker': 'Fortebet','competition': '','home': h_odd,'draw': d_odd,'away': a_odd,'sport': 'Football'})
            except:
                continue
        print(f"Fortebet: {football_count} matches extracted")
    except Exception as e:
        print(f"Fortebet error: {e}")
    return odds

def scrape_1xbet_mobile():
    odds = []
    try:
        print("Fetching 1xBet mobile API...")
        urls_to_try = [
            'https://mobile.1xbet.ug/LineFeed/Get1x2_Virt?sports=1&count=200&tf=2200000&tz=3&antisports=&regularChampionship=true&drawnGame=true&lang=en&afterId=0',
            'https://api.1xbet.ug/LineFeed/Get1x2_Virt?sports=1&count=200&tf=2200000&tz=3&lang=en',
            'https://1xbet.ug/en/LineFeed/Get1x2_Virt?sports=1&count=200&tf=2200000&tz=3&lang=en',
        ]
        headers = {
            'User-Agent': 'okhttp/4.9.0',
            'Accept': 'application/json',
            'X-App-Version': '1102070',
            'Accept-Language': 'en'
        }
        for url in urls_to_try:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode()
                data = json.loads(raw)
                print(f"1xBet mobile success: {url[:80]}")
                print(f"1xBet keys: {list(data.keys()) if isinstance(data,dict) else type(data)}")
                events = []
                if isinstance(data, dict):
                    events = data.get('Value', data.get('data', []))
                elif isinstance(data, list):
                    events = data
                print(f"1xBet events: {len(events)}")
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    home = event.get('O1','')
                    away = event.get('O2','')
                    if not home or not away:
                        continue
                    h_odd = d_odd = a_odd = None
                    for e in event.get('E', []):
                        t = e.get('T')
                        coef = e.get('C', 0)
                        if t == 1: h_odd = float(coef)
                        elif t == 2: d_odd = float(coef)
                        elif t == 3: a_odd = float(coef)
                    if h_odd and a_odd:
                        odds.append({'match': f"{home} vs {away}",'home_team': home,'away_team': away,'bookmaker': '1xBet','competition': '','home': h_odd,'draw': d_odd,'away': a_odd,'sport': 'Football'})
                if odds:
                    break
            except Exception as e:
                print(f"1xBet URL failed: {e}")
        print(f"1xBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"1xBet error: {e}")
    return odds

def scrape_betway_mobile():
    odds = []
    try:
        print("Fetching Betway mobile API...")
        urls_to_try = [
            'https://sports.betway.ug/api/pub/v2/categories/event-list?lang=en&country=UGA&eventPhase=Pre-match&categoryId=soccer&pageSize=100',
            'https://sports.betway.ug/api/pub/v2/events?lang=en&country=UGA&eventPhase=Pre-match&sport=soccer&pageSize=100',
            'https://betway.ug/api/pub/v2/event-list?sport=soccer&country=UGA&lang=en&pageSize=100',
        ]
        headers = {
            'User-Agent': 'BetwayApp/1.0 Android',
            'Accept': 'application/json',
            'Accept-Language': 'en-UG',
        }
        for url in urls_to_try:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read().decode()
                data = json.loads(raw)
                print(f"Betway mobile success: {url[:80]}")
                print(f"Betway keys: {list(data.keys()) if isinstance(data,dict) else type(data)}")
                events = []
                if isinstance(data, dict):
                    for key in ['events','data','matches','items','fixtures']:
                        if key in data and isinstance(data[key], list):
                            events = data[key]
                            break
                elif isinstance(data, list):
                    events = data
                print(f"Betway events: {len(events)}")
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    home = (event.get('homeTeam',{}).get('name','') or event.get('home',''))
                    away = (event.get('awayTeam',{}).get('name','') or event.get('away',''))
                    if not home or not away:
                        continue
                    h_odd = d_odd = a_odd = None
                    markets = event.get('markets', event.get('odds', []))
                    for market in markets:
                        selections = market.get('selections', market.get('outcomes', []))
                        if len(selections) >= 3:
                            h_odd = float(selections[0].get('price', selections[0].get('odds', 0)))
                            d_odd = float(selections[1].get('price', selections[1].get('odds', 0)))
                            a_odd = float(selections[2].get('price', selections[2].get('odds', 0)))
                            break
                    if h_odd and a_odd:
                        odds.append({'match': f"{home} vs {away}",'home_team': home,'away_team': away,'bookmaker': 'Betway','competition': '','home': h_odd,'draw': d_odd,'away': a_odd,'sport': 'Football'})
                if odds:
                    break
            except Exception as e:
                print(f"Betway URL failed: {e}")
        print(f"Betway: {len(odds)} matches extracted")
    except Exception as e:
        print(f"Betway error: {e}")
    return odds

def find_arbitrage(all_odds):
    opportunities = []
    STAKE = 100000
    matches = {}
    for odd in all_odds:
        key = odd['match'].lower().strip()
        if key not in matches:
            matches[key] = []
        matches[key].append(odd)
    for match_name, bookmakers in matches.items():
        if len(bookmakers) < 2:
            continue
        best_home = max(bookmakers, key=lambda x: x.get('home') or 0)
        best_draw = max(bookmakers, key=lambda x: x.get('draw') or 0)
        best_away = max(bookmakers, key=lambda x: x.get('away') or 0)
        h = best_home.get('home', 0)
        d = best_draw.get('draw', 0)
        a = best_away.get('away', 0)
        if not h or not a:
            continue
        arb2 = (1/h)+(1/a)
        if arb2 < 1:
            profit = round((1-arb2)*100, 2)
            opportunities.append({'match': match_name,'type': '2-way','profit_percent': profit,'bets': [{'bookmaker': best_home['bookmaker'],'outcome':'Home','odd': h,'stake': round(STAKE*(1/h)/arb2)},{'bookmaker': best_away['bookmaker'],'outcome':'Away','odd': a,'stake': round(STAKE*(1/a)/arb2)}]})
        if d:
            arb3 = (1/h)+(1/d)+(1/a)
            if arb3 < 1:
                profit = round((1-arb3)*100, 2)
                opportunities.append({'match': match_name,'type': '3-way','profit_percent': profit,'bets': [{'bookmaker': best_home['bookmaker'],'outcome':'Home','odd': h,'stake': round(STAKE*(1/h)/arb3)},{'bookmaker': best_draw['bookmaker'],'outcome':'Draw','odd': d,'stake': round(STAKE*(1/d)/arb3)},{'bookmaker': best_away['bookmaker'],'outcome':'Away','odd': a,'stake': round(STAKE*(1/a)/arb3)}]})
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
    print("Scraping 1xBet mobile...")
    xb = scrape_1xbet_mobile()
    all_odds.extend(xb)
    if xb: scraped.append('1xBet')
    print("Scraping Betway mobile...")
    bw = scrape_betway_mobile()
    all_odds.extend(bw)
    if bw: scraped.append('Betway')
    opportunities = find_arbitrage(all_odds)
    print(f"Found {len(opportunities)} arbitrage opportunities")
    output = {'last_updated': datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),'total_matches': len(all_odds),'bookmakers_scraped': scraped,'opportunities': opportunities,'raw_odds': all_odds}
    with open('odds.json', 'w') as f:
        json.dump(output, f, indent=2)
    print(f"Done! {len(all_odds)} matches saved")

if __name__ == '__main__':
    main()
