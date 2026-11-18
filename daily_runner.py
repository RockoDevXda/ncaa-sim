import os
import re
import io
import json
import time
import random
import difflib
import requests
import pandas as pd
from datetime import datetime

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
KENPOM_EMAIL = os.getenv("KENPOM_EMAIL", "")
KENPOM_PASSWORD = os.getenv("KENPOM_PASSWORD", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://rockodevxda.github.io/sports-command-center/")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

# ESPN -> KenPom exact naming bridge
ESPN_TO_KENPOM = {
    "ul monroe": "louisiana monroe",
    "ulm": "louisiana monroe",
    "ul lafayette": "louisiana",
    "uconn": "connecticut",
    "umass": "massachusetts",
    "unc": "north carolina",
    "unlv": "unlv",
    "usc": "usc",
    "smu": "smu",
    "tcu": "tcu",
    "vcu": "vcu",
    "ucf": "ucf",
    "utep": "utep",
    "utsa": "utsa",
    "lsu": "lsu",
    "ole miss": "mississippi",
    "miss state": "mississippi st",
    "mississippi state": "mississippi st",
    "pitt": "pittsburgh",
    "miami": "miami fl",
    "miami oh": "miami oh",
    "st johns": "st johns",
    "saint marys": "saint marys",
    "nc state": "north carolina st",
    "penn": "penn",
    "cal": "california",
    "siu edwardsville": "siue",
    "ualbany": "albany",
    "app state": "appalachian st",
    "sam houston": "sam houston st",
    "se louisiana": "southeastern louisiana",
    "ut martin": "tennessee martin",
    "ut arlington": "ut arlington",
    "ut rio grande valley": "utrgv",
    "uic": "illinois chicago",
    "umbc": "umbc",
    "umkc": "kansas city",
    "omaha": "nebraska omaha",
    "little rock": "little rock",
    "queens": "queens",
    "st thomas": "st thomas",
}

espn_stats_cache = {}


def normalize_for_kp(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"\d+$", "", s).strip()  # strip KenPom seed numbers if present
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\bstate\b", "st", s)
    s = re.sub(r"\s+", " ", s).strip()
    return ESPN_TO_KENPOM.get(s, s)


def fetch_kenpom_ratings():
    """Logs into KenPom and parses AdjO, AdjD, AdjT, and Luck for all 364 D1 teams."""
    if not KENPOM_EMAIL or not KENPOM_PASSWORD:
        print("KenPom credentials not set; falling back to ESPN efficiency.")
        return {}

    try:
        session = requests.Session()
        session.headers.update(HEADERS)

        # Load homepage first to initialize session cookies
        session.get("https://kenpom.com/", timeout=15)

        login_payload = {
            "email": KENPOM_EMAIL,
            "password": KENPOM_PASSWORD,
            "submit": "Login!",
        }
        login_resp = session.post(
            "https://kenpom.com/handlers/login_handler.php",
            data=login_payload,
            timeout=15,
        )
        login_resp.raise_for_status()

        home_resp = session.get("https://kenpom.com/index.php", timeout=15)
        home_resp.raise_for_status()

        tables = pd.read_html(io.StringIO(home_resp.text))
        if not tables:
            return {}

        df = tables[0]
        # Flatten multi-index columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[-1] for col in df.columns]

        ratings = {}
        for _, row in df.iterrows():
            team_raw = str(row.get("Team", "")).strip()
            if not team_raw or team_raw in ("Team", "nan"):
                continue

            # KenPom appends seed numbers to tournament teams (e.g., "Connecticut 1")
            team_clean = re.sub(r"\s+\d+$", "", team_raw).strip()
            norm_key = normalize_for_kp(team_clean)

            try:
                adj_em = float(row.get("AdjEM", row.get("NetRtg", 0.0)))
                adj_o = float(row.get("AdjO", row.get("ORtg", 105.0)))
                adj_d = float(row.get("AdjD", row.get("DRtg", 105.0)))
                adj_t = float(row.get("AdjT", 68.0))
                luck = float(row.get("Luck", 0.0))

                ratings[norm_key] = {
                    "kp_name": team_clean,
                    "adj_em": round(adj_em, 2),
                    "adj_o": round(adj_o, 1),
                    "adj_d": round(adj_d, 1),
                    "tempo": round(adj_t, 1),
                    "luck": round(luck, 3),
                    "source": "KenPom",
                }
            except (ValueError, TypeError):
                continue

        print(f"Successfully loaded {len(ratings)} teams from KenPom.")
        return ratings
    except Exception as e:
        print(f"KenPom fetch warning ({e}); falling back to ESPN stats.")
        return {}


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
            "away_location": away["team"].get("location", away["team"]["displayName"]),
            "away_record": away.get("records", [{}])[0].get("summary", "0-0"),
            "home_id": home["team"]["id"],
            "home_team": home["team"]["displayName"],
            "home_short": home["team"].get("shortDisplayName", home["team"]["displayName"]),
            "home_location": home["team"].get("location", home["team"]["displayName"]),
            "home_record": home.get("records", [{}])[0].get("summary", "0-0"),
        })
    return games


def fetch_espn_fallback(team_id, record_str):
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
                res = {"adj_o": adj_o, "adj_d": adj_d, "tempo": round(poss, 1), "luck": 0.0, "source": "ESPN"}
                espn_stats_cache[team_id] = res
                return res
    except Exception:
        pass

    res = {"adj_o": 95.0, "adj_d": 112.0, "tempo": 67.5, "luck": 0.0, "source": "Fallback"}
    espn_stats_cache[team_id] = res
    return res


def get_team_efficiency(game, side, kp_ratings):
    if kp_ratings:
        candidates = [
            game[f"{side}_location"],
            game[f"{side}_short"],
            game[f"{side}_team"],
        ]
        kp_keys = list(kp_ratings.keys())

        for c in candidates:
            norm = normalize_for_kp(c)
            if norm in kp_ratings:
                return kp_ratings[norm], True

        for c in candidates:
            norm = normalize_for_kp(c)
            matches = difflib.get_close_matches(norm, kp_keys, n=1, cutoff=0.84)
            if matches:
                return kp_ratings[matches[0]], True

    fb = fetch_espn_fallback(game[f"{side}_id"], game[f"{side}_record"])
    return fb, (fb["source"] == "ESPN")


def run_monte_carlo(game, kp_ratings, n_sims=10000):
    away_stats, away_ok = get_team_efficiency(game, "away", kp_ratings)
    home_stats, home_ok = get_team_efficiency(game, "home", kp_ratings)

    # KenPom Home-Court Advantage is ~3.4 pts per 100 possessions; 0.0 on neutral floors
    hca = 0.0 if game["neutral"] else 3.4
    exp_tempo = (away_stats["tempo"] + home_stats["tempo"]) / 2.0

    # KenPom efficiency expectation (National D1 average baseline = 106.0)
    league_avg = 106.0
    away_exp_eff = league_avg + (away_stats["adj_o"] - league_avg) + (home_stats["adj_d"] - league_avg)
    home_exp_eff = league_avg + (home_stats["adj_o"] - league_avg) + (away_stats["adj_d"] - league_avg) + hca

    away_wins = 0
    home_covers = 0
    away_covers = 0
    away_pts_total = 0.0
    home_pts_total = 0.0
    home_spread = game["home_spread"]

    for _ in range(n_sims):
        poss = random.gauss(exp_tempo, 2.7)
        a_pts = (random.gauss(away_exp_eff, 10.2) / 100.0) * poss
        h_pts = (random.gauss(home_exp_eff, 10.2) / 100.0) * poss
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

    verified_d1 = away_ok and home_ok and abs(home_spread) <= 24.5 and edge_pts <= 13.0
    rating_source = "KenPom" if (away_stats["source"] == "KenPom" and home_stats["source"] == "KenPom") else "ESPN"

    return {
        **game,
        "rating_source": rating_source,
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


def send_discord_report(results, kp_active):
    if not DISCORD_WEBHOOK_URL:
        return

    date_label = datetime.now().strftime("%b %d, %Y")
    if not results:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"username": "NCAA KenPom Engine", "content": f"🏀 **{date_label}:** No NCAA D1 games scheduled today."},
            timeout=15,
        )
        return

    pool = [g for g in results if g["verified_d1"]] or results
    lined_pool = [g for g in pool if g["has_line"]]

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

    source_badge = "KenPom AdjO/AdjD/Tempo" if kp_active else "ESPN Efficiency"
    embed_parlay = {
        "title": f"🎯 Top 4-Team Edge Parlay ({source_badge}) — {date_label}",
        "url": DASHBOARD_URL,
        "description": (
            f"**Combined Hit Rate:** `{parlay_pct}%` (**Fair Odds: {prob_to_american_odds(parlay_pct)}**)\n"
            f"10,000-run tempo-free simulation vs. Vegas lines. [Open Command Center]({DASHBOARD_URL})"
        ),
        "color": 3066993,
        "fields": parlay_fields,
        "footer": {"text": f"SimLab NCAA Engine • {source_badge}"},
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

    payload = {"username": "NCAA KenPom Engine", "embeds": [embed_parlay, embed_slate]}
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
    kp_ratings = fetch_kenpom_ratings()
    games = fetch_todays_games()
    results = [run_monte_carlo(g, kp_ratings, n_sims=10000) for g in games]

    with open("daily_slate.json", "w") as f:
        json.dump(results, f, indent=2)

    send_discord_report(results, kp_active=bool(kp_ratings))
    print(f"Completed {len(results)} simulations (KenPom active: {bool(kp_ratings)}).")
