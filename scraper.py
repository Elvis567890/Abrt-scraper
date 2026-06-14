import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime
import re
import time

def scrape_betpawa():
    odds = []
    try:
        session = requests.Session()
        headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        }
        session.headers.update(headers)
        session.get('https://www.betpawa.ug', timeout=30)
        time.sleep(2)
        url = 'https://www.betpawa.ug/events?categoryId=2&marketId=1X2'
        response = session.get(url, timeout=30)
        print(f"BetPawa status: {response.status_code}")
        print(f"BetPawa page size: {len(response.text)} bytes")
        soup = BeautifulSoup(response.text, 'html.parser')
        links = soup.find_all('a', href=re.compile(r'/event/\d+'))
        print(f"BetPawa: found {len(links)} event links")
        for link in links:
            try:
                text = link.get_text(separator='|', strip=True)
                parts = [p.strip() for p in text.split('|') if p.strip()]
                teams = []
                odd_values = []
                competition = ''
                skip = ['pm','am','Sat','Sun','Mon','Tue','Wed','Thu','Fri',
                        'Full Time','Half','1UP','2UP','1X2','Double','Both',
                        'Over','Under','Total','Score','Chance','Teams',
                        'Interval','minutes','First']
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
                    odds.append({
                        'match': f"{teams[0]} vs {teams[1]}",
                        'home_team': teams[0],
                        'away_team': teams[1],
                        'bookmaker': 'BetPawa',
                        'competition': competition,
                        'home': odd_values[0],
                        'draw': odd_values[1],
                        'away': odd_values[2],
                        'sport': 'Football',
                        'event_url': 'https://www.betpawa.ug' + link.get('href','')
                    })
            except:
                continue
        print(f"BetPawa: {len(odds)} matches extracted")
    except Exception as e:
        print(f"BetPawa error: {e}")
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
            opportunities.append({
                'match': match_name,
                'type': '2-way',
                'profit_percent': profit,
                'total_stake': STAKE,
                'bets': [
                    {'bookmaker': best_home['bookmaker'],'outcome': 'Home','team': best_home.get('home_team','Home'),'odd': h,'stake': round(STAKE*(1/h)/arb2)},
                    {'bookmaker': best_away['bookmaker'],'outcome': 'Away','team': best_away.get('away_team','Away'),'odd': a,'stake': round(STAKE*(1/a)/arb2)}
                ]
            })
        if d:
            arb3 = (1/h)+(1/d)+(1/a)
            if arb3 < 1:
                profit = round((1-arb3)*100, 2)
                opportunities.append({
                    'match': match_name,
                    'type': '3-way',
                    'profit_percent': profit,
                    'total_stake': STAKE,
                    'bets': [
                        {'bookmaker': best_home['bookmaker'],'outcome': 'Home','team': best_home.get('home_team','Home'),'odd': h,'stake': round(STAKE*(1/h)/arb3)},
                        {'bookmaker': best_draw['bookmaker'],'outcome': 'Draw','team': 'Draw','odd': d,'stake': round(STAKE*(1/d)/arb3)},
                        {'bookmaker': best_away['bookmaker'],'outcome': 'Away','team': best_away.get('away_team','Away'),'odd': a,'stake': round(STAKE*(1/a)/arb3)}
                    ]
                })
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
    opportunities = find_arbitrage(all_odds)
    print(f"Found {len(opportunities)} arbitrage opportunities")
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
