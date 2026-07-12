"""
ArbiScan TV
Single File Backend + Frontend

Run:
python app.py
"""

import os
import json
import threading
from datetime import datetime
from flask import Flask, jsonify, request


app = Flask(__name__)


# ==========================================================
# FILE STORAGE
# ==========================================================

ODDS_FILE = "odds.json"
OPPS_FILE = "opportunities.json"
HISTORY_FILE = "history.json"


def load_json(filename, default):

    if not os.path.exists(filename):
        return default

    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        return default



def save_json(filename, data):

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=4,
            ensure_ascii=False
        )



# ==========================================================
# PROFIT HISTORY ENGINE
# ==========================================================


def update_history(opportunities):

    history = load_json(
        HISTORY_FILE,
        {}
    )


    for item in opportunities:

        match = item.get(
            "match",
            "Unknown"
        )

        profit = float(
            item.get(
                "profit_percent",
                0
            )
        )


        if match not in history:

            history[match] = {

                "first_profit": profit,

                "highest_profit": profit,

                "current_profit": profit,

                "updates": 1,

                "timeline":[

                    {
                    "time":
                    datetime.now().isoformat(),

                    "profit":
                    profit
                    }

                ]

            }


        else:

            data = history[match]


            data["current_profit"] = profit


            if profit > data["highest_profit"]:

                data["highest_profit"] = profit



            data["updates"] += 1


            data["timeline"].append({

                "time":
                datetime.now().isoformat(),

                "profit":
                profit

            })


    save_json(
        HISTORY_FILE,
        history
    )


    return history



# ==========================================================
# API SECTION
# YOUR SCRAPER/API WILL CONNECT HERE
# ==========================================================


@app.route("/api/odds")
def api_odds():


    odds = load_json(
        ODDS_FILE,
        []
    )


    opportunities = load_json(
        OPPS_FILE,
        []
    )


    history = update_history(
        opportunities
    )


    bookmakers = list(
        set(
            [
            x.get("bookmaker")
            for x in odds
            if x.get("bookmaker")
            ]
        )
    )


    return jsonify({

        "raw_odds":
        odds,


        "opportunities":
        opportunities,


        "bookmakers_scraped":
        bookmakers,


        "total_matches":
        len(odds),


        "last_updated":
        datetime.now().isoformat()

    })



@app.route("/api/history/<path:match>")
def api_history(match):


    history = load_json(
        HISTORY_FILE,
        {}
    )


    return jsonify(
        history.get(
            match,
            {
                "first_profit":0,
                "highest_profit":0,
                "current_profit":0,
                "updates":0,
                "timeline":[]
            }
        )
    )



# ==========================================================
# BILLING PLACEHOLDER
# ==========================================================


@app.route("/api/billing")
def billing():

    return jsonify({

        "currency":"UGX",

        "plans":[

            {
            "name":"Free",
            "price":0
            },

            {
            "name":"Pro",
            "price":50000
            },

            {
            "name":"Enterprise",
            "price":"Custom"
            }

        ]

    })



# ==========================================================
# HOME PAGE
# FULL HTML WILL BE INSERTED IN PART 2
# ==========================================================


HTML = """

PLACEHOLDER_FRONTEND

"""


@app.route("/")
def home():

    return HTML



# ==========================================================
# SERVER
# ==========================================================


if __name__ == "__main__":

    print("="*50)

    print("📺 ArbiScan TV Started")

    print("🌐 http://localhost:5000")

    print("="*50)


    app.run(

        host="0.0.0.0",

        port=5000,

        debug=True

  )HTML = """

<!DOCTYPE html>
<html>

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width,initial-scale=1">

<title>ArbiScan TV</title>


<style>


*{
box-sizing:border-box;
font-family:
Arial,
Helvetica,
sans-serif;
}


body{

margin:0;

background:#050812;

color:white;

}


.header{

padding:20px;

background:#0b1220;

position:sticky;

top:0;

z-index:10;

border-bottom:
1px solid
rgba(255,255,255,.08);

}


.logo{

font-size:24px;

font-weight:800;

}


.sub{

font-size:12px;

color:#8993aa;

margin-top:5px;

}



.container{

padding:15px;

}



.hero{

background:
linear-gradient(
135deg,
#101b33,
#07101f
);

border-radius:25px;

padding:20px;

margin-bottom:15px;

}



.hero-title{

color:#9aa5bd;

font-size:14px;

}


.hero-profit{

font-size:38px;

font-weight:bold;

color:#00f5a0;

margin-top:8px;

}



.hero-match{

margin-top:8px;

color:#bbb;

}




.stats{

display:grid;

grid-template-columns:
1fr 1fr;

gap:12px;

}



.stat{

background:#11192c;

border-radius:20px;

padding:18px;

}



.stat-title{

font-size:12px;

color:#8892aa;

}


.stat-value{

font-size:25px;

font-weight:bold;

margin-top:5px;

}




.section{

margin-top:25px;

font-size:18px;

font-weight:bold;

}



.match-card{


background:#10182b;

border-radius:22px;

padding:18px;

margin-top:15px;

border:
1px solid
rgba(0,245,160,.15);

}



.match-top{

display:flex;

justify-content:
space-between;

align-items:center;

}



.match-name{

font-size:17px;

font-weight:bold;

}



.profit{


color:#00f5a0;

font-size:25px;

font-weight:bold;

}




.small{

color:#8993aa;

font-size:12px;

margin-top:6px;

}




.book{

background:#080e1c;

border-radius:14px;

padding:12px;

margin-top:10px;

display:flex;

justify-content:space-between;

font-size:13px;

}



.badge{

background:
rgba(0,245,160,.15);

color:#00f5a0;

padding:5px 10px;

border-radius:20px;

font-size:11px;

}




.modal{


display:none;

position:fixed;

left:0;

top:0;

width:100%;

height:100%;

background:
rgba(0,0,0,.85);

z-index:99;


}



.modal-box{


position:absolute;

bottom:0;

width:100%;

background:#10182b;

border-radius:
30px
30px
0
0;

padding:25px;

max-height:85%;

overflow:auto;

}




.close{

font-size:30px;

text-align:right;

cursor:pointer;

}



.history-item{

background:#080e1c;

padding:12px;

border-radius:15px;

margin-top:10px;

display:flex;

justify-content:space-between;

}



.green{

color:#00f5a0;

}


.orange{

color:#ff9900;

}



.bottom{


position:fixed;

bottom:0;

left:0;

right:0;

height:65px;

background:#080e1c;

display:flex;

justify-content:space-around;

align-items:center;

border-top:
1px solid
rgba(255,255,255,.1);


}



.nav{

font-size:12px;

color:#8892aa;

}



</style>

</head>


<body>


<div class="header">


<div class="logo">

📺 ArbiScan TV

</div>


<div class="sub">

Live Arbitrage Broadcast

</div>


</div>




<div class="container">



<div class="hero">


<div class="hero-title">

BEST OPPORTUNITY

</div>


<div class="hero-profit"
id="bestProfit">

0%

</div>


<div class="hero-match"
id="bestMatch">

Waiting...

</div>


</div>





<div class="stats">


<div class="stat">

<div class="stat-title">

MATCHES

</div>


<div class="stat-value"
id="matches">

0

</div>


</div>



<div class="stat">

<div class="stat-title">

BOOKMAKERS

</div>


<div class="stat-value"
id="books">

0

</div>


</div>


</div>



<div class="section">

Live Opportunities

</div>



<div id="list">

Loading...

</div>




</div>






<div id="modal"
class="modal">


<div class="modal-box">


<div class="close"
onclick="closeHistory()">

×

</div>


<div id="historyContent">

</div>



</div>


</div>




<div class="bottom">


<div class="nav">

Home

</div>


<div class="nav">

Arbs

</div>


<div class="nav">

Billing

</div>


<div class="nav">

Settings

</div>


</div>



<script>


let historyCache={};



async function loadData(){


let response =
await fetch(
"/api/odds?t="
+Date.now()
);


let data =
await response.json();



let arbs =
data.opportunities || [];



document.getElementById(
"matches"
).innerHTML =
arbs.length;



document.getElementById(
"books"
).innerHTML =
(data.bookmakers_scraped||[]).length;



let best =
arbs.sort(
(a,b)=>
b.profit_percent-
a.profit_percent
)[0];



if(best){


document.getElementById(
"bestProfit"
).innerHTML =
Number(best.profit_percent)
.toFixed(2)
+"%";


document.getElementById(
"bestMatch"
).innerHTML =
best.match;


}




let html="";



arbs.forEach(a=>{


historyCache[a.match]=a;



html += `


<div class="match-card"
onclick='openHistory(
${JSON.stringify(a)}
)'>



<div class="match-top">


<div class="match-name">

${a.match}

</div>


<div class="profit">

+${Number(a.profit_percent)
.toFixed(2)}%

</div>



</div>



<div class="small">

${a.type || ""}
</div>



${(a.bets||[])
.map(b=>`


<div class="book">


<span>

${b.bookmaker}

</span>


<span>

${b.outcome}

@
${b.odd}

</span>


</div>


`).join("")}



</div>


`;



});



document.getElementById(
"list"
).innerHTML =
html;



}




function openHistory(a){



let h =
a.history ||
{};



document.getElementById(
"modal"
).style.display="block";



document.getElementById(
"historyContent"
).innerHTML = `



<h2>

${a.match}

</h2>



<p>

First Profit:

<b class="green">

${h.first_profit || a.profit_percent}%

</b>

</p>



<p>

Current Profit:

<b>

${a.profit_percent}%

</b>

</p>



<p>

Times Updated:

<b class="orange">

${h.updates || 1}

</b>

</p>



<h3>

Timeline

</h3>


${(h.timeline||[])
.map(x=>`

<div class="history-item">

<span>

${x.time}

</span>


<span class="green">

${x.profit}%

</span>


</div>

`).join("")}



`;



}



function closeHistory(){

document.getElementById(
"modal"
).style.display="none";

}



loadData();



setInterval(
loadData,
30000
);



</script>


</body>

</html>


"""# ==========================================================
# OPTIONAL API CONNECTOR PLACE
# ADD YOUR API CALLS HERE LATER
# ==========================================================


@app.route("/api/update", methods=["POST"])
def update_data():

    """
    Your external API or scraper can POST data here.

    Example format:

    {
      "opportunities":[
        {
          "match":"Arsenal vs Man United",
          "profit_percent":45,
          "type":"3-way",
          "bets":[
            {
              "bookmaker":"Bet365",
              "outcome":"Arsenal",
              "odd":2.5
            }
          ]
        }
      ]
    }

    """

    data = request.json


    if not data:

        return jsonify({
            "success":False,
            "message":"No data received"
        })


    if "opportunities" in data:

        save_json(
            OPPS_FILE,
            data["opportunities"]
        )


    if "odds" in data:

        save_json(
            ODDS_FILE,
            data["odds"]
        )


    return jsonify({

        "success":True,

        "message":
        "Data updated"

    })




# ==========================================================
# HEALTH CHECK
# ==========================================================


@app.route("/api/status")
def status():

    return jsonify({

        "app":
        "ArbiScan TV",

        "status":
        "running",

        "time":
        datetime.now().isoformat()

    })




# ==========================================================
# START APPLICATION
# ==========================================================


if __name__ == "__main__":


    print("""

====================================

      📺 ArbiScan TV

      Live Arbitrage Dashboard

====================================

Server:
http://localhost:5000


Files:

odds.json
opportunities.json
history.json


API:

GET  /api/odds

GET  /api/history/<match>

POST /api/update


====================================

""")


    app.run(

        host="0.0.0.0",

        port=5000,

        debug=True

  )
