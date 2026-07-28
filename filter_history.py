import json
import os

# File paths
HISTORY_FILE = "arb_history.json"
FILTERED_HISTORY_FILE = "arb_history_filtered.json"
TOP_N = 50  # Keep only the top 50 opportunities (highest profit)

def filter_best_opportunities():
    if not os.path.exists(HISTORY_FILE):
        print("No history file found, skipping...")
        return
    
    # Load the history file
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        history = json.load(f)
    
    # Extract all opportunities with their profit
    opportunities = []
    for key, entry in history.items():
        if entry.get('valid', False):
            latest_version = entry.get('versions', [])[-1] if entry.get('versions') else None
            if latest_version:
                profit_percent = latest_version.get('profit_percent', 0)
                opportunities.append({
                    'key': key,
                    'entry': entry,
                    'profit_percent': profit_percent
                })
    
    # Sort by profit (highest first) and take top N
    opportunities.sort(key=lambda x: x['profit_percent'], reverse=True)
    top_opportunities = opportunities[:TOP_N]
    
    # Build new filtered history
    filtered_history = {}
    for opp in top_opportunities:
        filtered_history[opp['key']] = opp['entry']
    
    # Write filtered history
    with open(FILTERED_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(filtered_history, f, indent=2)
    
    # Replace the original file with the filtered version
    os.replace(FILTERED_HISTORY_FILE, HISTORY_FILE)
    
    print(f"✅ Filtered history: kept {len(filtered_history)} opportunities (top {TOP_N} by profit)")

if __name__ == "__main__":
    filter_best_opportunities()
