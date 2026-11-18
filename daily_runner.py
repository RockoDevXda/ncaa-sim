import os
import json
import time
import random
import requests
from datetime import datetime, timezone

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://rockodevxda.github.io/sports-command-center/")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

espn_stats_cache = {}


def load_previous_lines():
    """Reads existing daily_slate.json to preserve opening spreads & totals for line movement tracking."""
    if not os.path.exists("daily_slate.json"):
        return {}
    try:
        with open("daily_slate.json", "r") as f:
            prev = json.load(f)
        out = {}
        for g in prev:
            gid = str(g.get("id", ""))
            if gid:
                out[gid] = {
                    "open_line": g.get("open_line") or g.get("market_line"),
                    "open_spread": g.get("open_spread") if g.get("open_spread") is not None else g.get("home_spread"),
                    "open_total": g.get("open_total") if g.get("open_total") is not None else g.get("over_under"),
                }
        return out
    except Exception:
        return {}


def fetch_todays_games(prev_lines):
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

        away_short = away["team"].get("shortDisplayName", away["team"]["displayName"])
        home_short = home["team"].get("shortDisplayName", home["team"]["displayName"])

        odds_info = comp.get("odds", [{}])[0] if comp.get("odds") else {}
        spread_details = odds_info.get("details", "PK")
        home_spread = float(odds_info.get("spread", 0.0) or 0.0)
        game_ou = float(odds_info.get("overUnder", 0.0) or 0.0)
        has_line = bool(odds_info.get("details"))
        has_total = game_ou > 90.0

        gid = str(event["id"])
        prior = prev_lines.get(gid, {})

        # Preserve opening line if captured earlier today; otherwise set current as opening
        open_line = prior.get("open_line") if (prior.get("open_line") and prior.get("open_line") != "PK") else spread_details
        open_spread = prior.get("open_spread") if (prior.get("open_spread") not in (None, 0.0) or not has_line) else home_spread
        open_total = prior.get("open_total") if (prior.get("open_total") and prior.get("open_total") > 90.0) else game_ou

        spread_move = round(home_spread - float(open_spread or 0.0), 1) if has_line else 0.0
        total_move = round(game_ou - float(open_total or 0.0), 1) if (has_total and open_total and open_total > 90.0) else 0.0

        move_badges = []
        if has_line and abs(spread_move) >= 1.0 and open_line != spread_details:
            move_badges.append(f"📉 {open_line} → {spread_details}")
        if has_total and abs(total_move) >= 1.5:
            arrow = "⬆️" if total_move > 0 else "⬇️"
            move_badges.append(f"{arrow} O/U {open_total:g} → {game_ou:g}")

        tip_dt = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        tip_str = tip_dt.strftime("%I:%M %p UTC")

        games.append({
            "id": gid,
            "tipoff": tip_str,
            "venue": comp.get("venue", {}).get("fullName", "TBD"),
            "neutral": comp.get("neutralSite", False),
            "open_line": open_line,
            "open_spread": open_spread,
            "market_line": spread_details,
            "home_spread": home_spread,
            "spread_move": spread_move,
            "open_total": open_total,
            "over_under": game_ou,
            "total_move": total_move,
            "move_badges": move_badges,
            "has_move": len(move_badges) > 0,
            "has_line": has_line,
            "has_total": has_total,
            "away_id": away["team"]["id"],
            "away_team": away["team"]["displayName"],
            "away_short": away_short,
            "away_record": away.get("records", [{}])[0].get("summary", "0-0"),
            "home_id": home["team"]["id"],
            "home_team": home["team"]["displayName"],
            "home_short": home_short,
            "home_record": home.get("records", [{}])[0].get("summary", "0-0"),
        })
    return games


def fetch_espn_team_efficiency(team_id, record_str):
    if team_id in espn_stats_cache:
        return espn_stats_cache[team_id]

    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams/{team_id}/statistics"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            categories = resp.json().get("results", {}).get("stats", {}).get("categories", [])
            ppg, fga, fta, orb, tov = None, 58.0, 18.0, 10.0, 12.0
            for cat in categories:
                for stat in cat.get("stats", []):
                    n, v = stat.get("name", ""), float(stat.get("value", 0) or 0)
                    if n == "avgPoints":
                        ppg = v
                    elif n == "avgFieldGoalsAttempted":
                        fga = v
                    elif n == "avgFreeThrowsAttempted":
                        fta = v
                    elif n == "avgOffensiveRebounds":
                        orb = v
                    elif n == "avgTurnovers":
                        tov = v
            if ppg and ppg > 40:
                poss = max(60.0, min(78.0, fga - orb + tov + (0.44 * fta)))
                adj_o = round((ppg / poss) * 100.0, 1)
                parts = record_str.split("-")
                win_pct = int(parts[0]) / max(1, int(parts[0]) + int(parts[1])) if len(parts) == 2 else 0.5
                adj_d = round(107.0 - (win_pct * 8.5), 1)
                res = {"adj_o": adj_o, "adj_d": adj_d, "tempo": round(poss, 1), "verified": True}
                espn_stats_cache[team_id] = res
                return res
    except Exception:
        pass

    res = {"adj_o": 95.0, "adj_d": 112.0, "tempo": 67.5, "verified": False}
    espn_stats_cache[team_id] = res
    return res


def run_monte_carlo(game, n_sims=10000):
    away_stats = fetch_espn_team_efficiency(game["away_id"], game["away_record"])
    home_stats = fetch_espn_team_efficiency(game["home_id"], game["home_record"])

    hca = 0.0 if game["neutral"] else 3.3
    exp_tempo = (away_stats["tempo"] + home_stats["tempo"]) / 2.0

    league_avg = 105.5
    away_exp_eff = league_avg + (away_stats["adj_o"] - league_avg) + (home_stats["adj_d"] - league_avg)
    home_exp_eff = league_avg + (home_stats["adj_o"] - league_avg) + (away_stats["adj_d"] - league_avg) + hca

    away_wins = 0
    home_covers = 0
    away_covers = 0
    total_overs = 0
    total_unders = 0
    away_pts_total = 0.0
    home_pts_total = 0.0
    home_spread = game["home_spread"]
    game_ou = game["over_under"]

    for _ in range(n_sims):
        poss = random.gauss(exp_tempo, 2.7)
        a_pts = (random.gauss(away_exp_eff, 10.2) / 100.0) * poss
        h_pts = (random.gauss(home_exp_eff, 10.2) / 100.0) * poss
        comb_pts = a_pts + h_pts

        away_pts_total += a_pts
        home_pts_total += h_pts

        if a_pts > h_pts:
            away_wins += 1

        if (h_pts + home_spread) > a_pts:
            home_covers += 1
        elif (h_pts + home_spread) < a_pts:
            away_covers += 1

        if game["has_total"]:
            if comb_pts > game_ou:
                total_overs += 1
            elif comb_pts < game_ou:
                total_unders += 1

    away_proj = round(away_pts_total / n_sims, 1)
    home_proj = round(home_pts_total / n_sims, 1)
    total_proj = round(away_proj + home_proj, 1)

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

    if game["has_total"]:
        over_pct = round((total_overs / n_sims) * 100, 1)
        under_pct = round((total_unders / n_sims) * 100, 1)
        total_edge_pts = round(abs(total_proj - game_ou), 1)
        if over_pct >= under_pct:
            total_pick = f"O {game_ou:g}"
            total_cover_pct = over_pct
        else:
            total_pick = f"U {game_ou:g}"
            total_cover_pct = under_pct
    else:
        total_pick = "No Total"
        total_cover_pct = 0.0
        total_edge_pts = 0.0

    verified_d1 = away_stats["verified"] and home_stats["verified"] and abs(home_spread) <= 24.5 and edge_pts <= 13.0

    return {
        **game,
        "rating_source": "ESPN",
        "verified_d1": verified_d1,
        "away_proj": away_proj,
        "home_proj": home_proj,
        "total_proj": total_proj,
        "away_win_pct": away_prob,
        "home_win_pct": home_prob,
        "favored_team": favored_team,
        "favored_prob": favored_prob,
        "spread_pick": spread_pick,
        "spread_cover_pct": spread_cover_pct,
        "edge_pts": edge_pts,
        "total_pick": total_pick,
        "total_cover_pct": total_cover_pct,
        "total_edge_pts": total_edge_pts,
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

    pool = [g for g in results if g["verified_d1"]] or results
    lined_pool = [g for g in pool if g["has_line"]]
    totals_pool = [g for g in pool if g["has_total"] and g["total_edge_pts"] <= 14.0]

    top_spreads = sorted(lined_pool, key=lambda x: x["spread_cover_pct"], reverse=True)
    top_totals = sorted(totals_pool, key=lambda x: x["total_cover_pct"], reverse=True)
    top_ml = sorted(pool, key=lambda x: x["favored_prob"], reverse=True)

    parlay_legs = []
    used_ids = set()

    for g in top_spreads[:2]:
        move_note = f" • {' '.join(g['move_badges'])}" if g.get("move_badges") else ""
        parlay_legs.append({
            "game": f"{g['away_short']} @ {g['home_short']}",
            "pick": g["spread_pick"],
            "prob": g["spread_cover_pct"],
            "note": f"+{g['edge_pts']} pt edge vs `{g['market_line']}` (Sim: {g['away_proj']}-{g['home_proj']}){move_note}",
        })
        used_ids.add(g["id"])

    for g in top_totals:
        if len(parlay_legs) >= 3:
            break
        if g["id"] not in used_ids:
            parlay_legs.append({
                "game": f"{g['away_short']} @ {g['home_short']}",
                "pick": f"{g['home_short']}/{g['away_short']} {g['total_pick']}",
                "prob": g["total_cover_pct"],
                "note": f"Sim Total: `{g['total_proj']}` vs O/U `{g['over_under']}` (+{g['total_edge_pts']} edge)",
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
        "title": f"🎯 Top 4-Team NCAA Edge Parlay (ATS + Totals) — {date_label}",
        "url": DASHBOARD_URL,
        "description": (
            f"**Combined Hit Rate:** `{parlay_pct}%` (**Fair Odds: {prob_to_american_odds(parlay_pct)}**)\n"
            f"10,000-run simulation vs. live Vegas lines & totals. [Open Command Center]({DASHBOARD_URL})"
        ),
        "color": 3066993,
        "fields": parlay_fields,
        "footer": {"text": "SimLab NCAA Engine • ESPN Line-Move Telemetry"},
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    movers = [g for g in results if g.get("has_move")]
    slate_source = movers if len(movers) >= 6 else results
    slate_title = f"📉 Sharp Line Movers ({len(movers)} Games Moved)" if len(movers) >= 6 else f"📋 Full Slate Projections ({len(results)} Games)"

    slate_lines = [
        f"• **{g['away_short']}** `{g['away_proj']}` @ **{g['home_short']}** `{g['home_proj']}` | Tot `{g['total_proj']}` `[{g['market_line']} | {g['total_pick']}]` {' '.join(g.get('move_badges', []))}"
        for g in slate_source[:30]
    ]
    embed_slate = {
        "title": slate_title,
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
    prev_lines = load_previous_lines()
    games = fetch_todays_games(prev_lines)
    results = [run_monte_carlo(g, n_sims=10000) for g in games]

    with open("daily_slate.json", "w") as f:
        json.dump(results, f, indent=2)

    send_discord_report(results)
    movers_cnt = sum(1 for g in results if g.get("has_move"))
    print(f"Completed {len(results)} NCAA simulations ({movers_cnt} line moves detected).")
