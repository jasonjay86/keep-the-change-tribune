"""
power_rankings.py — compute Composite Power Score and Matchup of the Week.

Formula (per config.json weights):
  PowerScore = w_win * W%
             + w_pf  * (PFpg / league_avg_PFpg)
             + w_sos * SoS_factor
             + w_ap  * AllPlayW%

Components:
  W%       = current win rate
  PFpg     = points scored per game played
  SoS      = average PFpg of opponents faced, scaled by league average
  AllPlay% = record if every team played every other team every week

Final PowerScore is normalized so #1 = 100 and last = 40, keeping the spread
readable. Inputs and intermediate stats are exposed for the LLM commentary.

Usage: python power_rankings.py [--in data.json] [--out rankings.json]
"""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

def build_user_map(users):
    """Returns dict[user_id] -> {display_name, team_name, avatar}."""
    return {
        u["user_id"]: {
            "display_name": u.get("display_name") or u.get("username") or "Unknown",
            "team_name":    (u.get("metadata") or {}).get("team_name") or "",
            "avatar":       u.get("avatar"),
        }
        for u in users
    }

def roster_stats(roster, user_map):
    """Extract normalized fields from a Sleeper roster object."""
    s = roster.get("settings") or {}
    # Sleeper stores PF as fpts (whole points *1000) + fpts_decimal
    fpts = (s.get("fpts") or 0) + ((s.get("fpts_decimal") or 0) / 1000.0)
    fpa  = (s.get("fpts_against") or 0) + ((s.get("fpts_against_decimal") or 0) / 1000.0)
    wins   = s.get("wins", 0)
    losses = s.get("losses", 0)
    ties   = s.get("ties", 0)
    games  = wins + losses + ties
    return {
        "roster_id":     roster["roster_id"],
        "owner_id":      roster.get("owner_id"),
        "owner":         user_map.get(roster.get("owner_id"), {}),
        "wins":          wins,
        "losses":        losses,
        "ties":          ties,
        "games":         games,
        "points_for":    fpts,
        "points_against": fpa,
        "win_pct":       (wins + 0.5 * ties) / games if games else 0.0,
        "pf_per_game":   fpts / games if games else 0.0,
    }

def all_play_win_pct(my_roster_id, all_matchups, pf_per_game_by_team, league_avg_pf):
    """
    Approximate AllPlay% by counting, for every past week, how many teams each
    roster would have beaten and lost to under median scoring.
    Sleeper's /matchups endpoint gives each roster's points_for for that week;
    AllPlay wins = weeks you scored above the median of all teams.
    """
    weeks_seen = defaultdict(dict)  # weeks_seen[week][roster_id] = points
    for wk in sorted(all_matchups.keys()):
        for entry in all_matchups[wk]:
            weeks_seen[wk][entry["roster_id"]] = entry.get("points") or 0

    wins = 0
    total_matchups = 0
    for wk, scores in weeks_seen.items():
        if my_roster_id not in scores:
            continue
        my_score = scores[my_roster_id]
        others = [v for k, v in scores.items() if k != my_roster_id]
        if not others:
            continue
        median = sorted(others)[len(others) // 2]
        # beat each team above median, lose to each below, split ties at median
        for opp_score in others:
            total_matchups += 1
            if my_score > opp_score:
                wins += 1
            elif my_score == opp_score:
                wins += 0.5
    return (wins / total_matchups) if total_matchups else 0.0

def collect_all_matchups(league_id):
    """Fetch every week of matchups for SoS + AllPlay. Cheap read on public API."""
    import urllib.request
    base = "https://api.sleeper.app/v1"
    all_wk = {}
    for wk in range(1, 19):  # regular season cap; playoffs loop separately if needed
        try:
            with urllib.request.urlopen(f"{base}/league/{league_id}/matchups/{wk}", timeout=15) as r:
                data = json.loads(r.read())
        except Exception:
            break
        if not data:
            break
        all_wk[wk] = data
    return all_wk

def strength_of_schedule(my_roster_id, all_matchups, pf_per_game_by_team, league_avg_pf):
    """
    SoS_factor = average PFpg of opponents faced, divided by league_avg_PFpg.
    Above 1.0 = tougher schedule; below 1.0 = softer.
    """
    opps_pf = []
    for wk, entries in all_matchups.items():
        # Find this roster's matchup_id, then the opposing roster
        my_entry = next((e for e in entries if e["roster_id"] == my_roster_id), None)
        if not my_entry:
            continue
        matchup_id = my_entry.get("matchup_id")
        if matchup_id is None:
            continue
        opp_entry = next(
            (e for e in entries if e["roster_id"] != my_roster_id and e.get("matchup_id") == matchup_id),
            None,
        )
        if not opp_entry:
            continue
        opp_rid = opp_entry["roster_id"]
        if opp_rid in pf_per_game_by_team and pf_per_game_by_team[opp_rid] > 0:
            opps_pf.append(pf_per_game_by_team[opp_rid])
    if not opps_pf:
        return 1.0
    return (sum(opps_pf) / len(opps_pf)) / league_avg_pf if league_avg_pf else 1.0

def compute_rankings(bundle: dict, weights: dict, all_matchups: dict | None = None) -> dict:
    users   = bundle["users"]
    rosters = bundle["rosters"]
    league_id = bundle["league"]["league_id"]

    user_map = build_user_map(users)
    enriched = [roster_stats(r, user_map) for r in rosters]

    played = [r for r in enriched if r["games"] > 0]
    if not played:
        # Season hasn't started — return the table with zeroed scores so the
        # LLM can still produce an "Opening Day" edition. Compute a schedule
        # preview by alphabetical pair-fallback since there's nothing to rank
        # by yet.
        schedule_pairs = defaultdict(set)
        for m in (bundle.get("matchups") or []):
            if m.get("matchup_id") is None:
                continue
            schedule_pairs[m["matchup_id"]].add(m["roster_id"])
        preview_motw = None
        if schedule_pairs and enriched:
            first_pair = next(iter(schedule_pairs.values()))
            rids = sorted(first_pair)
            a = next((r for r in enriched if r["roster_id"] == rids[0]), None)
            b = next((r for r in enriched if r["roster_id"] == rids[1]), None)
            if a and b:
                preview_motw = {
                    "status": "preview",
                    "team_a": {"name": a["owner"]["display_name"], "team": a["owner"]["team_name"], "rank": 0, "score": 50.0, "record": "0-0"},
                    "team_b": {"name": b["owner"]["display_name"], "team": b["owner"]["team_name"], "rank": 0, "score": 50.0, "record": "0-0"},
                }
        return {
            "league": bundle["league"]["name"],
            "week":   bundle["nfl_state"].get("week"),
            "season": bundle["league"]["season"],
            "season_type": bundle["nfl_state"].get("season_type"),
            "rankings": [
                {**r,
                 "pf_per_game": 0,
                 "sos_factor": 1.0,
                 "all_play_pct": 0.5,
                 "raw_score": 0.0,
                 "power_score": 50.0,
                 "rank": i + 1}
                for i, r in enumerate(enriched)
            ],
            "matchup_of_week": preview_motw,
        }

    pf_per_game_by_team = {r["roster_id"]: r["pf_per_game"] for r in enriched}
    league_avg_pf = sum(pf_per_game_by_team.values()) / len(pf_per_game_by_team)

    if all_matchups is None:
        all_matchups = collect_all_matchups(league_id)

    for r in enriched:
        r["sos_factor"]   = strength_of_schedule(r["roster_id"], all_matchups, pf_per_game_by_team, league_avg_pf)
        r["all_play_pct"] = all_play_win_pct(r["roster_id"], all_matchups, pf_per_game_by_team, league_avg_pf)

    # Raw composite
    for r in enriched:
        pf_norm = (r["pf_per_game"] / league_avg_pf) if league_avg_pf else 1.0
        r["raw_score"] = (
            weights["win_pct"]            * r["win_pct"]
          + weights["pf_per_game"]        * pf_norm
          + weights["strength_of_schedule"] * r["sos_factor"]
          + weights["all_play_win_pct"]   * r["all_play_pct"]
        )

    ranked = sorted(enriched, key=lambda x: x["raw_score"], reverse=True)

    # Normalize: #1 = 100, last = 40
    top = ranked[0]["raw_score"]
    bot = ranked[-1]["raw_score"]
    spread = top - bot if top != bot else 1.0
    for i, r in enumerate(ranked):
        r["power_score"] = 100 - ((top - r["raw_score"]) / spread) * 60  # 100 → 40
        r["rank"] = i + 1

    # Matchup of the Week: top-2 if they play each other this week; else
    # top-ranked team vs. highest-ranked available opponent
    week = bundle["nfl_state"].get("week", 1)
    matchups = bundle.get("matchups") or []
    week_pairs = defaultdict(set)
    for m in matchups:
        if m.get("matchup_id") is None:
            continue
        week_pairs[m["matchup_id"]].add(m["roster_id"])

    top_two = (ranked[0]["roster_id"], ranked[1]["roster_id"])
    motw = None
    motw_status = "preview"  # "preview" = scheduled but no scores yet; "live" = played

    # When no matchup_ids are present (pre-game week), week_pairs is empty.
    # Pair rosters by index — Sleeper returns matchups as alternating pairs
    # in the same order across weeks, so [0,1] vs [2,3] etc.
    if not week_pairs and matchups:
        by_idx = sorted(matchups, key=lambda m: m.get("roster_id", 0))
        for i in range(0, len(by_idx) - 1, 2):
            week_pairs[i // 2].add(by_idx[i]["roster_id"])
            week_pairs[i // 2].add(by_idx[i + 1]["roster_id"])
        motw_status = "preview"

    for pair in week_pairs.values():
        if top_two[0] in pair and top_two[1] in pair:
            motw = (ranked[0], ranked[1])
            break
    if motw is None and ranked:
        for pair in week_pairs.values():
            if ranked[0]["roster_id"] in pair:
                opp_rid = next(r for r in pair if r != ranked[0]["roster_id"])
                opp = next((x for x in ranked if x["roster_id"] == opp_rid), None)
                if opp:
                    motw = (ranked[0], opp)
                break

    return {
        "league": bundle["league"]["name"],
        "week":   week,
        "season": bundle["league"]["season"],
        "season_type": bundle["nfl_state"].get("season_type"),
        "rankings": ranked,
        "matchup_of_week": (
            {
                "status": motw_status,
                "team_a": {"name": motw[0]["owner"]["display_name"],
                           "team":  motw[0]["owner"]["team_name"],
                           "rank":  motw[0]["rank"],
                           "score": motw[0]["power_score"],
                           "record": f"{motw[0]['wins']}-{motw[0]['losses']}" + (f"-{motw[0]['ties']}" if motw[0]['ties'] else "")},
                "team_b": {"name": motw[1]["owner"]["display_name"],
                           "team":  motw[1]["owner"]["team_name"],
                           "rank":  motw[1]["rank"],
                           "score": motw[1]["power_score"],
                           "record": f"{motw[1]['wins']}-{motw[1]['losses']}" + (f"-{motw[1]['ties']}" if motw[1]['ties'] else "")},
            } if motw else None
        ),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data.json")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="rankings.json")
    args = ap.parse_args()

    bundle = json.loads(Path(args.inp).read_text())
    cfg    = json.loads(Path(args.config).read_text())
    result = compute_rankings(bundle, cfg["weights"])

    Path(args.out).write_text(json.dumps(result, indent=2))
    print(f"[power_rankings] wrote {args.out}")
    print(f"  {len(result['rankings'])} teams ranked, week {result['week']} ({result['season_type']})")
    for r in result["rankings"]:
        owner = r["owner"]["display_name"] if r["owner"] else "?"
        print(f"  #{r['rank']:>2}  {owner:<20}  Power {r['power_score']:5.1f}  "
              f"W% {r['win_pct']:.3f}  PFpg {r['pf_per_game']:>6.1f}  "
              f"SoS {r['sos_factor']:.2f}  AP% {r['all_play_pct']:.3f}")
    if result["matchup_of_week"]:
        m = result["matchup_of_week"]
        print(f"  Matchup of the Week: #{m['team_a']['rank']} {m['team_a']['name']} vs #{m['team_b']['rank']} {m['team_b']['name']}")
    else:
        print("  Matchup of the Week: (none — bye or off-week)")

if __name__ == "__main__":
    main()