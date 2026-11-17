Python
import os
import json
import time
import random
import requests
from datetime import datetime

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://yourusername.github.io/ncaa-sim/")


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

        # Extract sportsbook spread/odds if ESPN provides them
        odds_info = comp.get("odds", [{}])[0] if comp.get("odds") else {}
        spread_details = odds_info.get("details", "N/A")

        games.append({
            "id": event["id"],
            "tipoff": event["date"],
            "venue": comp.get("venue", {}).get("fullName", "TBD"),
            "neutral_site": comp.get("neutralSite", False),
            "market_line": spread_details,
            "away_team": away["team"]["displayName"],
            "away_short": away["team"].get("shortDisplayName", away["team"]["displayName"]),
            "away_record": away.get("records", [{}])[0].get("summary", "0-0"),
            "home_team": home["team"]["displayName"],
            "home_short": home["team"].get("shortDisplayName", home["team"]["displayName"]),
            "home_record": home.get("records", [{}])[0].get("summary", "0-0"),
        })
    return games


def fetch_efficiency_ratings():
    """Pulls current season BartTorvik team efficiency metrics (AdjO, AdjD, Tempo)."""
    year = datetime.now().year if datetime.now().month < 7 else datetime.now().year + 1
    url = f"https://barttorvik.com/{year}_team_results.json"
    try:
        data = requests.get(url, timeout=15).json()
        return {
            row[1]: {
                "adj_o": float(row[4]),
                "adj_d": float(row[6]),
                "tempo": float(row[15]),
            }
            for row in data
        }
    except Exception:
        return {}


def run_monte_carlo(game, ratings, n_sims=10000):
    away_stats = ratings.get(game["away_short"], ratings.get(game["away_team"], {"adj_o": 105.0, "adj_d": 105.0, "tempo": 68.0}))
    home_stats = ratings.get(game["home_short"], ratings.get(game["home_team"], {"adj_o": 105.0, "adj_d": 105.0, "tempo": 68.0}))

    hca = 0.0 if game["neutral_site"] else 3.2
    exp_tempo = (away_stats["tempo"] + home_stats["tempo"]) / 2.0

    away_exp_eff = 105.0 + (away_stats["adj_o"] - 105.0) + (home_stats["adj_d"] - 105.0)
    home_exp_eff = 105.0 + (home_stats["adj_o"] - 105.0) + (away_stats["adj_d"] - 105.0) + hca

    away_wins = 0
    away_pts_total = 0.0
    home_pts_total = 0.0

    for _ in range(n_sims):
        poss = random.gauss(exp_tempo, 2.8)
        a_pts = (random.gauss(away_exp_eff, 10.5) / 100.0) * poss
        h_pts = (random.gauss(home_exp_eff, 10.5) / 100.0) * poss
        away_pts_total += a_pts
        home_pts_total += h_pts
        if a_pts > h_pts:
            away_wins += 1

    away_prob = round((away_wins / n_sims) * 100, 1)
    home_prob = round(100.0 - away_prob, 1)
    favored_team = game["away_short"] if away_prob >= home_prob else game["home_short"]
    favored_prob = max(away_prob, home_prob)

    return {
        **game,
        "away_proj": round(away_pts_total / n_sims, 1),
        "home_proj": round(home_pts_total / n_sims, 1),
        "away_win_pct": away_prob,
        "home_win_pct": home_prob,
        "favored_team": favored_team,
        "favored_prob": favored_prob,
        "generated_at": datetime.now().isoformat(),
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
        print("DISCORD_WEBHOOK_URL not set; skipping Discord notification.")
        return

    date_label = datetime.now().strftime("%b %d, %Y")
    if not results:
        payload = {
            "username": "NCAA Monte Carlo Engine",
            "content": f"🏀 **{date_label}:** No NCAA D1 games scheduled on today's slate.",
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        return

    # Sort games by highest simulated win probability
    ranked = sorted(results, key=lambda x: x["favored_prob"], reverse=True)
    top_edges = ranked[:6]

    # Build Top 3 Parlay calculation
    top_3 = ranked[:3]
    parlay_prob = 1.0
    for g in top_3:
        parlay_prob *= (g["favored_prob"] / 100.0)
    parlay_pct = round(parlay_prob * 100, 1)
    parlay_odds = prob_to_american_odds(parlay_pct)

    # Embed 1: Top Simulated Edges & Suggested 3-Leg Parlay
    top_fields = []
    for g in top_edges:
        neutral_tag = " *(Neutral)*" if g["neutral_site"] else ""
        top_fields.append({
            "name": f"🔥 {g['favored_team']} ({g['favored_prob']}% | Fair {prob_to_american_odds(g['favored_prob'])})",
            "value": (
                f"**{g['away_short']}** ({g['away_proj']}) @ **{g['home_short']}** ({g['home_proj']}){neutral_tag}\n"
                f"Line: `{g['market_line']}` • Venue: {g['venue']}"
            ),
            "inline": False,
        })

    parlay_legs_str = " + ".join([f"**{g['favored_team']}** ({g['favored_prob']}%)" for g in top_3])
    top_fields.append({
        "name": f"🎯 Top 3-Leg Model Parlay — Hit Rate: {parlay_pct}% ({parlay_odds})",
        "value": parlay_legs_str,
        "inline": False,
    })

    embed_top = {
        "title": f"🏀 NCAA D1 Monte Carlo Slate — {date_label}",
        "url": DASHBOARD_URL,
        "description": f"Simulated **{len(results)} games** × `10,000` runs each. [Open Interactive Web Dashboard]({DASHBOARD_URL})",
        "color": 3066993,  # Emerald Green (#2ECC71)
        "fields": top_fields,
        "footer": {"text": "NCAA Monte Carlo Engine • Updated Automatically at 12:15 AM CST"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    # Embed 2: Compact Full-Slate Summary (keeps well under Discord's 4096-char description cap)
    slate_lines = []
    for g in results[:35]:
        slate_lines.append(
            f"• **{g['away_short']}** `{g['away_proj']}` ({g['away_win_pct']}%) @ "
            f"**{g['home_short']}** `{g['home_proj']}` ({g['home_win_pct']}%)"
        )
    if len(results) > 35:
        slate_lines.append(f"*...plus {len(results) - 35} more games in attached `daily_slate.json`*")

    embed_slate = {
        "title": f"📋 Full Slate Projections ({len(results)} Games)",
        "description": "\n".join(slate_lines)[:3900],
        "color": 3447003,  # Royal Blue (#3498DB)
    }

    payload = {
        "username": "NCAA Monte Carlo Engine",
        "embeds": [embed_top, embed_slate],
    }

    # Send with multipart file attachment + 429 rate-limit handling
    with open("daily_slate.json", "rb") as json_file:
        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": ("daily_slate.json", json_file, "application/json"),
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=20)
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 2)
            time.sleep(float(retry_after))
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
    print(f"Completed {len(results)} simulations and posted to Discord.")
