from flask import Flask, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)

HISTORY_FILE = "arbitrage_history.json"


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "message": "Arbitrage Backend Online"
    })


@app.route("/api/opportunities")
def opportunities():

    history = load_history()

    data = []

    for key, item in history.items():

        if item.get("invalid_since"):
            continue

        history_list = item.get("history", [])

        data.append({

            "match":
                item.get("match"),

            "sport":
                item.get("sport"),

            "type":
                item.get("type"),

            "profit_percent":
                item.get("profit_percent", 0),

            "initial_profit":
                item.get(
                    "initial_profit",
                    item.get("profit_percent", 0)
                ),

            "profit_ugx":
                item.get("profit_ugx", 0),

            "total_stake":
                item.get("total_stake", 0),

            "arb_sum":
                item.get("arb_sum", 0),

            "bets":
                item.get("bets", []),

            "update_count":
                item.get(
                    "update_count",
                    len(history_list)
                ),

            "first_seen":
                item.get("first_seen"),

            "last_seen":
                item.get("last_seen"),

            "history":
                history_list
        })

    data.sort(
        key=lambda x: x["profit_percent"],
        reverse=True
    )

    return jsonify(data)


@app.route("/api/stats")
def stats():

    history = load_history()

    active = []

    for item in history.values():

        if item.get("invalid_since"):
            continue

        active.append(item)

    total_profit = sum(
        x.get("profit_ugx", 0)
        for x in active
    )

    highest = 0

    if active:
        highest = max(
            x.get("profit_percent", 0)
            for x in active
        )

    return jsonify({

        "total_opportunities":
            len(active),

        "highest_profit":
            highest,

        "estimated_profit_ugx":
            total_profit,

        "server_time":
            datetime.utcnow().isoformat()
    })


@app.route("/api/match/<path:name>")
def single_match(name):

    history = load_history()

    for item in history.values():

        if item.get("match") == name:

            return jsonify(item)

    return jsonify({
        "error": "Match not found"
    }), 404


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
        )
