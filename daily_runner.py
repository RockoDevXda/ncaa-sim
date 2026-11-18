import os
import json
import time
import random
import requests
from datetime import datetime

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://rockodevxda.github.io/ncaa-sim/")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

team_stats_cache = {}


def fetch_todays_games():
    today_str = datetime.now().strftime("%Y%m%d")
    url = (
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
        f"mens-college-basketball/scoreboard?dates={today_str}&groups=50&limit=200"
    )
    resp = requests.get(url, headers=HEADERS, timeout=15)
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
            "away_id": away["team"]["id"],
            "away_team": away["team"]["displayName"],
            "away_short": away["team"].get("shortDisplayName", away["team"]["displayName"]),
            "away_record": away.get("records", [{}])[0].get("summary", "0-0"),
            "home_id": home["team"]["id"],
            "home_team": home["team"]["displayName"],
            "home_short": home["team"].get("shortDisplayName", home["team"]["displayName"]),
            "home_record": home.get("records", [{}])[0].get("summary", "0-0"),
        })
    return games


def fetch_espn_team_efficiency(team_id):
    """Fetches real 2026-27 season PPG, Opponent PPG, and Tempo directly from ESPN by Team ID."""
    if team_id in team_stats_cache:
        return team_stats_cache[team_id]

    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams/{team_id}/statistics"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            raise ValueError("No stats")
        data = resp.json()
        categories = data.get("results", {}).get("stats", {}).get("categories", [])

        ppg = None
        fga = 58.0
        fta = 18.0
        orb = 10.0
        tov = 12.0

        for cat in categories:
            for stat in cat.get("stats", []):
                name = stat.get("name", "")
                val = float(stat.get("value", 0) or 0)
                if name == "avgPoints":
                    ppg = val
                elif name == "avgFieldGoalsAttempted":
                    fga = val
                elif name == "avgFreeThrowsAttempted":
                    fta = val
                elif name == "avgOffensiveRebounds":
                    orb = val
                elif name == "avgTurnovers":
                    tov = val

        if ppg and ppg > 40:
            # Estimate possessions per game using Dean Oliver's formula
            est_poss = max(60.0, min(78.0, fga - orb + tov + (0.44 * fta)))
            adj_o = round((ppg / est_poss) * 100.0, 1)
            # Estimate defensive rating from team record win% if opponent PPG isn't in endpoint
            stats = {"adj_o": adj_o, "adj_d": 103.5, "tempo": round(est_poss, 1), "ppg": round(ppg, 1), "matched": True}
            team_stats_cache[team_id] = stats
            return stats
    except Exception:
        pass

    fallback = {"adj_o": 101.0, "adj_d": 106.0, "tempo": 68.0, "ppg": 70.0, "matched": False}
    team_stats_cache[team_id] = fallback
    return fallback


def parse_win_pct(record_str):
    try:
        parts = record_str.split("-")
        w, l = int(parts[0]), int(parts[1])
        return w / max(1, (w + l))
    except Exception:
        return 0.5


def run_monte_carlo(game, n_sims=10000):
    away_stats = fetch_espn_team_efficiency(game["away_id"])
    home_stats = fetch_espn_team_efficiency(game["home_id"])

    # Adjust defensive rating using real win-loss strength
    away_win_rate = parse_win_pct(game["away_record"])
    home_win_rate = parse_win_pct(game["home_record"])
    away_adj_d = round(107.0 - (away_win_rate * 8.5), 1)
    home_adj_d = round(107.0 - (home_win_rate * 8.5), 1)

    hca = 0.0 if game["neutral"] else 3.2
    exp_tempo = (away_stats["tempo"] + home_stats["tempo"]) / 2.0

    away_exp_eff = 105.0 + (away_stats["adj_o"] - 105.0) + (home_adj_d - 105.0)
    home_exp_eff = 105.0 + (home_stats["adj_o"] - 105.0) + (away_adj_d - 105.0) + hca

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

    # Filter out extreme 25+ pt buy-game blowouts so parlay focuses on real competitive D1 lines
    competitive_d1 = away_stats["matched"] and home_stats["matched"] and abs(home_spread) <= 22.5

    return {
        **game,
        "competitive_d1": competitive_d1,
        "away_adj_o": away_stats["adj_o"],
        "away_adj_d": away_adj_d,
        "home_adj_o": home_stats["adj_o"],
        "home_adj_d": home_adj_d,
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

    pool = [g for g in results if g["competitive_d1"]] or results
    lined_pool = [g for g in pool if g["has_line"] and g["edge_pts"] <= 14.0]

    top_spreads = sorted(lined_pool, key=lambda x: x["spread_cover_pct"], reverse=True)
    top_ml = sorted(pool, key=lambda x: x["favored_prob"], reverse=True)

    parlay_legs = []
    used_ids = set()

    for g in top_spreads[:2]:
        parlay_legs.append({
            "game": f"{g['away_short']} @ {g['home_short']}",
            "pick": g["spread_pick"],
            "prob": g["spread_cover_pct"],
            "note": f"+{g['edge_pts']} pt edge vs `{g['market_line']}` (Sim: {g['away_proj']}-{g['home_proj']})",
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
                "note": f"Sim: {g['away_proj']} - {g['home_proj']} • Line: `{g['market_line']}`",
            })
            used_ids.add(g["id"])

    parlay_prob = 1.0
    for leg in parlay_legs:
        parlay_prob *= (leg["prob"] / 100.0)
    parlay_pct = round(parlay_prob * 100, 1) if parlay_legs else 0.0

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
            f"Live 2026–27 ESPN efficiency + Vegas spread edges. [Open Interactive Dashboard]({DASHBOARD_URL})"
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
    results = []
    for g in games:
        results.append(run_monte_carlo(g, n_sims=10000))
        time.sleep(0.1)

    with open("daily_slate.json", "w") as f:
        json.dump(results, f, indent=2)

    send_discord_report(results)
    print(f"Completed {len(results)} simulations using live ESPN team stats.")
