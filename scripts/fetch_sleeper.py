"""
fetch_sleeper.py — pull league data from the public Sleeper API.

Usage: python fetch_sleeper.py [--out data.json]
Writes JSON bundle with league, users, rosters, matchups, nfl_state,
and a resolved players index (slim — only players actually on a roster).
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://api.sleeper.app/v1"
PLAYERS_CACHE = Path("cache/players_nfl.json")
PLAYERS_CACHE_MAX_AGE_DAYS = 7


def _get(path: str):
    url = f"{BASE}{path}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def fetch_players_db(force: bool = False) -> dict:
    """
    Fetch the full NFL players database (~14MB, 12k players) and cache it
    locally. Used to resolve player_id -> {name, position, team} for the
    MOTW's highlighted-players feature.
    """
    if not force and PLAYERS_CACHE.exists():
        age_days = (time.time() - PLAYERS_CACHE.stat().st_mtime) / 86400
        if age_days < PLAYERS_CACHE_MAX_AGE_DAYS:
            return json.loads(PLAYERS_CACHE.read_text())

    print(f"[fetch_sleeper] downloading NFL player database (~14MB)...", file=sys.stderr)
    with urllib.request.urlopen(f"{BASE}/players/nfl", timeout=60) as r:
        data = r.read()
    PLAYERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PLAYERS_CACHE.write_bytes(data)
    return json.loads(data)


def slim_player(player: dict) -> dict:
    """Keep only the fields we actually render."""
    return {
        "name":      f"{player.get('first_name', '')} {player.get('last_name', '')}".strip(),
        "position":  player.get("position") or "?",
        "team":      player.get("team") or "FA",
        "status":    player.get("status") or "Active",
        "injury":    player.get("injury_status"),
        "years_exp": player.get("years_exp"),  # 0 = rookie, 1+ = experienced
        "age":       player.get("age"),
    }


def fetch_weekly_projections(season: int, week: int) -> dict:
    """
    Sleeper publishes per-player weekly projections at
    /projections/nfl/regular/{season}/{week}. Each entry has fields like
    pts_half_ppr, pts_ppr, pts_std, plus the stat breakdown.
    Returns {player_id: projection_dict} for the ~600 players on the slate.
    """
    try:
        data = _get(f"/projections/nfl/regular/{season}/{week}")
    except Exception as e:
        print(f"[fetch_sleeper] WARN: projections fetch failed ({e}); falling back to zero projections", file=sys.stderr)
        return {}
    return data


def projected_points_for_team(roster: dict, projections: dict, scoring: str = "pts_half_ppr",
                               league_scoring: dict | None = None) -> float:
    """
    Sum projected fantasy points for a roster's STARTERS (the lineup actually
    fielded in a given week). `scoring` selects the primary offense scoring
    field ('pts_half_ppr' for half PPR, 'pts_std' for standard, 'pts_ppr'
    for full PPR). If `league_scoring` is provided, IDP scoring is applied
    on top — matching what Sleeper shows on the league page.

    Sleeper's pre-computed `pts_*` fields are offense-only (QB/RB/WR/TE/K).
    IDP scoring (tackles, sacks, ints, etc.) is computed separately from
    raw projection stats weighted by league settings. Without league_scoring
    this function returns offense-only points — which under-reports IDP-heavy
    leagues.
    """
    starters = roster.get("starters") or []
    total = 0.0
    for pid in starters:
        proj = projections.get(pid)
        if not proj:
            continue
        # Offense scoring from the pre-computed field — covers QB/RB/WR/TE/K
        # with all standard yardage, TD, reception, and bonus weights baked in.
        pts = proj.get(scoring)
        if isinstance(pts, (int, float)):
            total += float(pts)
        # IDP scoring from raw stats weighted by league settings.
        # Only idp_* fields are added here; standard offense stats are
        # already covered by pts_half_ppr above (avoid double-counting).
        if league_scoring:
            for stat_key, weight in league_scoring.items():
                if not stat_key.startswith("idp_"):
                    continue
                if not isinstance(weight, (int, float)) or weight == 0:
                    continue
                stat_val = proj.get(stat_key)
                if isinstance(stat_val, (int, float)):
                    total += float(stat_val) * float(weight)
    return round(total, 2)


def starter_projected_points(roster: dict, projections: dict, scoring: str = "pts_half_ppr",
                             league_scoring: dict | None = None) -> dict[str, float]:
    """
    Per-starter projected points for a roster's STARTERS. Returns
    {player_id: projected_points} for every starter that has a projection.
    Used by power_rankings.py to identify the highest-projected players
    on each MOTW side (instead of defaulting to the QB+RB1 that Sleeper
    puts first in lineup order).

    Scoring logic mirrors projected_points_for_team: offense via the
    pre-computed pts_* field, IDP via raw stats weighted by league settings.
    """
    out: dict[str, float] = {}
    starters = roster.get("starters") or []
    for pid in starters:
        proj = projections.get(pid)
        if not proj:
            continue
        total = 0.0
        pts = proj.get(scoring)
        if isinstance(pts, (int, float)):
            total += float(pts)
        if league_scoring:
            for stat_key, weight in league_scoring.items():
                if not stat_key.startswith("idp_"):
                    continue
                if not isinstance(weight, (int, float)) or weight == 0:
                    continue
                stat_val = proj.get(stat_key)
                if isinstance(stat_val, (int, float)):
                    total += float(stat_val) * float(weight)
        if total > 0:
            out[pid] = round(total, 2)
    return out


def fetch(league_id: str) -> dict:
    league = _get(f"/league/{league_id}")
    users = _get(f"/league/{league_id}/users")
    rosters = _get(f"/league/{league_id}/rosters")

    # Current week + season status from the NFL state endpoint
    nfl_state = _get("/state/nfl")
    week = nfl_state.get("week", 1)
    season = nfl_state.get("season") or league.get("season") or 2026

    matchups = _get(f"/league/{league_id}/matchups/{week}")

    # Last-week matchups — used by the LLM commentary to recap the
    # *previous* week's action in the lede (and to surface interesting
    # stats in the by-the-numbers boxes). Pulled only when we're past
    # week 1 of the regular season; preseason has no prior week to recap.
    last_week_matchups = []
    last_week = None
    season_type = league.get("season_type", "regular")
    if season_type == "regular" and week > 1:
        last_week = week - 1
        try:
            last_week_matchups = _get(f"/league/{league_id}/matchups/{last_week}") or []
        except Exception as e:
            print(f"[fetch_sleeper] could not fetch last-week (wk{last_week}) matchups: {e}", file=sys.stderr)
            last_week_matchups = []

    # Slim player index — only players actually on someone's roster
    all_player_ids = set()
    for r in rosters:
        all_player_ids.update(r.get("players") or [])
        all_player_ids.update(r.get("starters") or [])

    players_db = fetch_players_db()
    players_index = {
        pid: slim_player(players_db[pid])
        for pid in all_player_ids
        if pid in players_db
    }

    # Weekly projections — used to compute per-team projected points and
    # hence a real point spread even when no games have been played yet.
    # League scoring comes from league.settings (e.g. best ball, half PPR).
    projections = fetch_weekly_projections(season, week)
    if projections:
        # KTC is half-PPR per the 2026 league config; keep that explicit.
        # Pass league_scoring so IDP and other custom scoring weights are
        # applied — without this, IDP-heavy leagues under-report projections
        # (offense-only pts_half_ppr misses tackles/sacks/ints/etc.).
        scoring_field = "pts_half_ppr"
        league_scoring = league.get("scoring_settings") or {}
        rosters_by_id = {r["roster_id"]: r for r in rosters}
        # Attach per-matchup projected scores so the pick step can compute spread
        for m in matchups:
            rid = m.get("roster_id")
            roster = rosters_by_id.get(rid) or {}
            m["projected_points"] = projected_points_for_team(
                roster, projections, scoring_field, league_scoring
            )
            # Also stash per-starter projected points so power_rankings.py
            # can highlight the actual top-projected players (not just the
            # QB+RB1 that Sleeper puts first in lineup order). This drives
            # the MOTW blurb and Tribune Pick player names.
            m["starter_projected_points"] = starter_projected_points(
                roster, projections, scoring_field, league_scoring
            )

        # Group matchups by matchup_id and attach projected_spread per pair
        from collections import defaultdict
        by_matchup = defaultdict(list)
        for m in matchups:
            by_matchup[m.get("matchup_id")].append(m)
        for mid, pair in by_matchup.items():
            if len(pair) == 2:
                pts_pair = sorted([p.get("projected_points", 0.0) for p in pair], reverse=True)
                # Spread = top - bottom, attached to whichever team is favored
                spread = round(pts_pair[0] - pts_pair[1], 2)
                for p in pair:
                    p["opponent_projected_points"] = pts_pair[1] if p.get("projected_points") == pts_pair[0] else pts_pair[0]
                    # Favored team gets a NEGATIVE spread (Vegas convention from the LLM prompt)
                    if p.get("projected_points") == pts_pair[0]:
                        p["projected_spread"] = -spread  # favorite: negative
                    else:
                        p["projected_spread"] = spread   # underdog: positive
                # Same spread on both sides
                for p in pair:
                    p["matchup_projected_spread"] = spread

    return {
        "league": league,
        "users": users,
        "rosters": rosters,
        "matchups": matchups,
        "last_week_matchups": last_week_matchups,
        "last_week": last_week,
        "nfl_state": nfl_state,
        "players_index": players_index,
        "projections_available": bool(projections),
        "scoring_format": "half_ppr",
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league-id", default="1312862141639839744")
    ap.add_argument("--out", default="data.json")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    config_path = Path(args.config)
    if config_path.exists():
        cfg = json.loads(config_path.read_text())
    else:
        cfg = {}

    league_id = cfg.get("league_id", args.league_id)

    try:
        bundle = fetch(league_id)
    except Exception as e:
        print(f"[fetch_sleeper] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    Path(args.out).write_text(json.dumps(bundle, indent=2))
    print(f"[fetch_sleeper] wrote {args.out}")
    print(f"  league: {bundle['league'].get('name')!r}")
    print(f"  season: {bundle['league'].get('season')}  status: {bundle['league'].get('status')}")
    print(f"  users: {len(bundle['users'])}  rosters: {len(bundle['rosters'])}  matchups_wk{bundle['nfl_state'].get('week')}: {len(bundle['matchups'])}")

if __name__ == "__main__":
    main()