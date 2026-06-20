import json
from datetime import datetime
from playwright.sync_api import sync_playwright
import re
import urllib.request

def scrape_betpawa():
    odds = []
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
            api_data = []
            def handle_response(response):
                try:
                    if response.status == 200:
                        ct = response.headers.get('content-type','')
                        url = response.url
                        if 'json' in ct and any(x in url for x in ['event','odds','match','sport','market']):
                            data = response.json()
                            api_data.append({'url': url, 'data': data})
                            print(f"BetPawa API: {url[:100]}")
                except:
                    pass
            page.on('response', handle_response)
            print("Opening BetPawa...")
            page.goto('https://www.betpawa.ug/events?categoryId=2&marketId=1X2', timeout=60000)
            page.wait_for_timeout(8000)
            html = page.content()
            print(f"BetPawa loaded: {len(html)} bytes")
            print(f"BetPawa API calls: {len(api_data)}")
            # Try API first
            api_extracted = 0
            for item in api_data:
                try:
                    d = item['data']
                    events = []
                    if isinstance(d, dict):
                        for key in ['events','data','matches','items','results']:
                            if key in d and isinstance(d[key], list):
                                events = d[key]
                                break
                    elif isinstance(d, list):
                        events = d
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        home = event.get('homeTeamName') or event.get('home_team') or event.get('homeName','')
                        away = event.get('awayTeamName') or event.get('away_team') or event.get('awayName','')
                        if not home or not away:
                            continue
                        markets = event.get('markets') or event.get('odds') or []
                        for market in markets:
                            if not isinstance(market, dict):
                                continue
                            selections = market.get('selections') or market.get('outcomes') or []
                            if len(selections) >= 3:
                                h = float(selections[0].get('odds') or selections[0].get('price') or 0)
                                d2 = float(selections[1].get('odds') or selections[1].get('price') or 0)
                                a = float(selections[2].get('odds') or selections[2].get('price') or 0)
                                if h and a:
                                    api_extracted += 1
                                    odds.append({'match': f"{home} vs {away}",'home_team': home,'away_team': away,'bookmaker': 'BetPawa','competition': '','home': h,'draw': d2,'away': a,'sport': 'Football'})
                                    break
                except:
                    continue
            print(f"BetPawa API extracted: {api_extracted}")
            # Fallback to HTML scraping if API gave nothing
            if api_extracted == 0:
                links = page.query_selector_all('a[href*="/event/"], a[href*="/match/"]')
                print(f"BetPawa fallback: found {len(links)} links")
                skip = ['pm','am','Sat','Sun','Mon','Tue','Wed','Thu','Fri','Full Time','Half','1UP','2UP','1X2','Double','Both','Over','Under','Total','Score','Chance','Teams','Interval','minutes','First']
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
                            odds.append({'match': f"{teams[0]} vs {teams[1]}",'home_team': teams[0],'away_team': teams[1],'bookmaker': 'BetPawa','competition': competition,'home': odd_values[0],'draw': odd_values[1],'away': odd_values[2],'sport': 'Football'})
                    except:
                        continue
            browser.close()
            print(f"BetPawa: {len(odds)} matches extracted")
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

def scrape_1xbet():
    odds = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-blink-features=AutomationControlled'])
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                locale='en-UG'
            )
            page = context.new_page()
            api_data = []
            def handle_response(response):
                try:
                    if response.status == 200:
                        ct = response.headers.get('content-type','')
                        url = response.url
                        if 'json' in ct and any(x in url for x in ['LineFeed','line','odds','sport','soccer','football']):
                            data = response.json()
                            api_data.append({'url': url, 'data': data})
                            print(f"1xBet API: {url[:100]}")
                except:
                    pass
            page.on('response', handle_response)
            print("Opening 1xBet...")
            page.goto('https://1xbet.ug/en/line/football', timeout=60000)
            page.wait_for_timeout(10000)
            html = page.content()
            print(f"1xBet loaded: {len(html)} bytes")
            print(f"1xBet API calls: {len(api_data)}")
            for item in api_data:
                try:
                    d = item['data']
                    events = []
                    if isinstance(d, dict):
                        events = d.get('Value', d.get('data', d.get('events', [])))
                    elif isinstance(d, list):
                        events = d
                    if not isinstance(events, list):
                        continue
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        home = event.get('O1') or event.get('team1') or event.get('home','')
                        away = event.get('O2') or event.get('team2') or event.get('away','')
                        if not home or not away:
                            continue
                        h_odd = d_odd = a_odd = None
                        e_list = event.get('E', [])
                        for e in e_list:
                            t = e.get('T')
                            coef = e.get('C', 0)
                            if t == 1: h_odd = float(coef)
                            elif t == 2: d_odd = float(coef)
                            elif t == 3: a_odd = float(coef)
                        if h_odd and a_odd:
                            odds.append({'match': f"{home} vs {away}",'home_team': home,'away_team': away,'bookmaker': '1xBet','competition': '','home': h_odd,'draw': d_odd,'away': a_odd,'sport': 'Football'})
                except:
                    continue
            browser.close()
            print(f"1xBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"1xBet error: {e}")
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
    if bp:
        scraped.append('BetPawa')
    print("Scraping Fortebet...")
    fb = scrape_fortebet()
    all_odds.extend(fb)
    if fb:
        scraped.append('Fortebet')
    print("Scraping 1xBet...")
    xb = scrape_1xbet()
    all_odds.extend(xb)
    if xb:
        scraped.append('1xBet')
    opportunities = find_arbitrage(all_odds)
    print(f"Found {len(opportunities)} arbitrage opportunities")
    output = {'last_updated': datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),'total_matches': len(all_odds),'bookmakers_scraped': scraped,'opportunities': opportunities,'raw_odds': all_odds}
    with open('odds.json', 'w') as f:
        json.dump(output, f, indent=2)
    print(f"Done! {len(all_odds)} matches saved")

if __name__ == '__main__':
    main()
