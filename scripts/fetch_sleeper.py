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
        "name":     f"{player.get('first_name', '')} {player.get('last_name', '')}".strip(),
        "position": player.get("position") or "?",
        "team":     player.get("team") or "FA",
        "status":   player.get("status") or "Active",
        "injury":   player.get("injury_status"),
    }


def fetch(league_id: str) -> dict:
    league = _get(f"/league/{league_id}")
    users = _get(f"/league/{league_id}/users")
    rosters = _get(f"/league/{league_id}/rosters")

    # Current week + season status from the NFL state endpoint
    nfl_state = _get("/state/nfl")
    week = nfl_state.get("week", 1)

    matchups = _get(f"/league/{league_id}/matchups/{week}")

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

    return {
        "league": league,
        "users": users,
        "rosters": rosters,
        "matchups": matchups,
        "nfl_state": nfl_state,
        "players_index": players_index,
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