import os
import re
import json
import time
import random
import difflib
import requests
from datetime import datetime

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://rockodevxda.github.io/ncaa-sim/")

# Common ESPN -> BartTorvik naming aliases
TEAM_ALIASES = {
    "ul monroe": "louisiana monroe",
    "ulm": "louisiana monroe",
    "ul lafayette": "louisiana",
    "uconn": "connecticut",
    "umass": "massachusetts",
    "unc": "north carolina",
    "unlv": "nevada las vegas",
    "usc": "southern california",
    "smu": "southern methodist",
    "tcu": "texas christian",
    "vcu": "virginia commonwealth",
    "ucf": "central florida",
    "utep": "texas el paso",
    "utsa": "texas san antonio",
    "lsu": "louisiana state",
    "ole miss": "mississippi",
    "miss state": "mississippi state",
    "pitt": "pittsburgh",
    "miami": "miami fl",
    "st johns": "st johns",
    "saint marys": "saint marys",
    "nc state": "north carolina state",
    "penn": "pennsylvania",
    "cal": "california",
}


def normalize_name(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\bst\b", "state", s)
    s = re.sub(r"\s+", " ", s).strip()
    return TEAM_ALIASES.get(s, s)


def fetch_todays_games():
    today_str = datetime.now().strftime("%Y%m%d")
    url = (
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
        f"mens-college-basketball/scoreboard?dates={today_str}&groups=50&limit=200"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    games = []
    for event in data.get("events", []):
        comp = event["competitions"][0]
        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        odds_info = comp.get("odds", [{}])[0] if comp.get("odds") else {}
        spread_details = odds_info.get("details", "PK")
        home_spread = float(odds_info.get("spread", 0.0) or 0.0)
        has_line = bool(odds_info.get("details"))

        tip_dt = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        tip_str = tip_dt.strftime("%I:%M %p UTC")

        games.append({
            "id": event["id"],
            "tipoff": tip_str,
            "venue": comp.get("venue", {}).get("fullName", "TBD"),
            "neutral": comp.get("neutralSite", False),
            "market_line": spread_details,
            "home_spread": home_spread,
            "has_line": has_line,
            "away_team": away["team"]["displayName"],
            "away_short": away["team"].get("shortDisplayName", away["team"]["displayName"]),
            "away_location": away["team"].get("location", away["team"]["displayName"]),
            "away_record": away.get("records", [{}])[0].get("summary", "0-0"),
            "home_team": home["team"]["displayName"],
            "home_short": home["team"].get("shortDisplayName", home["team"]["displayName"]),
            "home_location": home["team"].get("location", home["team"]["displayName"]),
            "home_record": home.get("records", [{}])[0].get("summary", "0-0"),
        })
    return games


def fetch_efficiency_ratings():
    year = datetime.now().year if datetime.now().month < 7 else datetime.now().year + 1
    url = f"https://barttorvik.com/{year}_team_results.json"
    try:
        data = requests.get(url, timeout=15).json()
        ratings = {}
        for row in data:
            norm = normalize_name(row[1])
            ratings[norm] = {
                "adj_o": round(float(row[4]), 1),
                "adj_d": round(float(row[6]), 1),
                "tempo": round(float(row[15]), 1),
            }
        return ratings
    except Exception:
        return {}


def lookup_team_stats(game, side, ratings):
    candidates = [
        game[f"{side}_location"],
        game[f"{side}_short"],
        game[f"{side}_team"],
    ]
    all_keys = list(ratings.keys())

    for c in candidates:
        norm = normalize_name(c)
        if norm in ratings:
            return ratings[norm], True

    # Fuzzy match fallback (e.g., "alcorn state" vs "alcorn st")
    for c in candidates:
        norm = normalize_name(c)
        matches = difflib.get_close_matches(norm, all_keys, n=1, cutoff=0.82)
        if matches:
            return ratings[matches[0]], True

    # Unmatched / Non-D1 fallback (penalized baseline so non-D1 teams don't look like D1 average)
    return {"adj_o": 94.0, "adj_d": 114.0, "tempo": 67.5}, False


def run_monte_carlo(game, ratings, n_sims=10000):
    away_stats, away_matched = lookup_team_stats(game, "away", ratings)
    home_stats, home_matched = lookup_team_stats(game, "home", ratings)

    hca = 0.0 if game["neutral"] else 3.2
    exp_tempo = (away_stats["tempo"] + home_stats["tempo"]) / 2.0

    away_exp_eff = 105.0 + (away_stats["adj_o"] - 105.0) + (home_stats["adj_d"] - 105.0)
    home_exp_eff = 105.0 + (home_stats["adj_o"] - 105.0) + (away_stats["adj_d"] - 105.0) + hca

    away_wins = 0
    home_covers = 0
    away_covers = 0
    away_pts_total = 0.0
    home_pts_total = 0.0
    home_spread = game["home_spread"]

    for _ in range(n_sims):
        poss = random.gauss(exp_tempo, 2.8)
        a_pts = (random.gauss(away_exp_eff, 10.5) / 100.0) * poss
        h_pts = (random.gauss(home_exp_eff, 10.5) / 100.0) * poss
        away_pts_total += a_pts
        home_pts_total += h_pts

        if a_pts > h_pts:
            away_wins += 1

        if (h_pts + home_spread) > a_pts:
            home_covers += 1
        elif (h_pts + home_spread) < a_pts:
            away_covers += 1

    away_proj = round(away_pts_total / n_sims, 1)
    home_proj = round(home_pts_total / n_sims, 1)
    away_prob = round((away_wins / n_sims) * 100, 1)
    home_prob = round(100.0 - away_prob, 1)

    favored_team = game["away_short"] if away_prob >= home_prob else game["home_short"]
    favored_prob = max(away_prob, home_prob)

    home_cover_pct = round((home_covers / n_sims) * 100, 1)
    away_cover_pct = round((away_covers / n_sims) * 100, 1)

    sim_home_margin = home_proj - away_proj
    vegas_home_margin = -home_spread
    edge_pts = round(abs(sim_home_margin - vegas_home_margin), 1)

    if home_cover_pct >= away_cover_pct:
        spread_pick = f"{game['home_short']} {home_spread:+g}" if home_spread != 0 else f"{game['home_short']} PK"
        spread_cover_pct = home_cover_pct
    else:
        away_spread = -home_spread
        spread_pick = f"{game['away_short']} {away_spread:+g}" if away_spread != 0 else f"{game['away_short']} PK"
        spread_cover_pct = away_cover_pct

    # Only eligible for parlay value picks if both teams matched real D1 ratings & spread isn't a 35+ pt blowout
    verified_d1 = away_matched and home_matched and abs(home_spread) <= 24.5 and edge_pts <= 12.0

    return {
        **game,
        "verified_d1": verified_d1,
        "away_adj_o": away_stats["adj_o"],
        "away_adj_d": away_stats["adj_d"],
        "home_adj_o": home_stats["adj_o"],
        "home_adj_d": home_stats["adj_d"],
        "tempo": round(exp_tempo, 1),
        "away_proj": away_proj,
        "home_proj": home_proj,
        "away_win_pct": away_prob,
        "home_win_pct": home_prob,
        "favored_team": favored_team,
        "favored_prob": favored_prob,
        "spread_pick": spread_pick,
        "spread_cover_pct": spread_cover_pct,
        "edge_pts": edge_pts,
        "generated_at": datetime.now().strftime("%Y-%m-%d %I:%M %p"),
    }


def prob_to_american_odds(prob_pct):
    p = prob_pct / 100.0
    if p <= 0 or p >= 1:
        return "N/A"
    if p >= 0.5:
        return f"{int(round(-100 * p / (1 - p)))}"
    return f"+{int(round(100 * (1 - p) / p))}"


def send_discord_report(results):
    if not DISCORD_WEBHOOK_URL:
        return

    date_label = datetime.now().strftime("%b %d, %Y")
    if not results:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"username": "NCAA Monte Carlo Engine", "content": f"🏀 **{date_label}:** No NCAA D1 games scheduled today."},
            timeout=15,
        )
        return

    verified_lined = [g for g in results if g["has_line"] and g["verified_d1"]]
    top_spreads = sorted(verified_lined, key=lambda x: x["spread_cover_pct"], reverse=True)
    top_ml = sorted([g for g in results if g["verified_d1"]], key=lambda x: x["favored_prob"], reverse=True)

    parlay_legs = []
    used_ids = set()

    for g in top_spreads[:2]:
        parlay_legs.append({
            "game": f"{g['away_short']} @ {g['home_short']}",
            "pick": g["spread_pick"],
            "prob": g["spread_cover_pct"],
            "note": f"+{g['edge_pts']} pt model edge vs {g['market_line']} (Sim: {g['away_proj']}-{g['home_proj']})",
        })
        used_ids.add(g["id"])

    for g in top_ml:
        if len(parlay_legs) >= 4:
            break
        if g["id"] not in used_ids:
            parlay_legs.append({
                "game": f"{g['away_short']} @ {g['home_short']}",
                "pick": f"{g['favored_team']} ML",
                "prob": g["favored_prob"],
                "note": f"Sim: {g['away_proj']} - {g['home_proj']} • Line: {g['market_line']}",
            })
            used_ids.add(g["id"])

    parlay_prob = 1.0
    for leg in parlay_legs:
        parlay_prob *= (leg["prob"] / 100.0)
    parlay_pct = round(parlay_prob * 100, 1)

    parlay_fields = [
        {
            "name": f"Leg {i+1}: {leg['pick']} ({leg['prob']}% | Fair {prob_to_american_odds(leg['prob'])})",
            "value": f"**{leg['game']}** • {leg['note']}",
            "inline": False,
        }
        for i, leg in enumerate(parlay_legs)
    ]

    embed_parlay = {
        "title": f"🎯 Top 4-Team Edge Parlay — {date_label}",
        "url": DASHBOARD_URL,
        "description": (
            f"**Combined Hit Rate:** `{parlay_pct}%` (**Fair Odds: {prob_to_american_odds(parlay_pct)}**)\n"
            f"Verified D1 efficiency matchups (top ATS edges + ML anchors). [Open Interactive Dashboard]({DASHBOARD_URL})"
        ),
        "color": 3066993,
        "fields": parlay_fields,
        "footer": {"text": "NCAA Monte Carlo Engine • Auto-Updated at 12:15 AM CST"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    slate_lines = [
        f"• **{g['away_short']}** `{g['away_proj']}` ({g['away_win_pct']}%) @ **{g['home_short']}** `{g['home_proj']}` ({g['home_win_pct']}%) `[{g['market_line']}]`"
        for g in results[:35]
    ]
    embed_slate = {
        "title": f"📋 Full Slate Projections ({len(results)} Games)",
        "description": "\n".join(slate_lines)[:3900],
        "color": 3447003,
    }

    payload = {"username": "NCAA Monte Carlo Engine", "embeds": [embed_parlay, embed_slate]}
    with open("daily_slate.json", "rb") as json_file:
        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": ("daily_slate.json", json_file, "application/json"),
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=20)
        if resp.status_code == 429:
            time.sleep(float(resp.json().get("retry_after", 2)))
            json_file.seek(0)
            requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=20)
        else:
            resp.raise_for_status()


if __name__ == "__main__":
    games = fetch_todays_games()
    ratings = fetch_efficiency_ratings()
    results = [run_monte_carlo(g, ratings, n_sims=10000) for g in games]

    with open("daily_slate.json", "w") as f:
        json.dump(results, f, indent=2)

    send_discord_report(results)
    print(f"Completed {len(results)} verified simulations.")
