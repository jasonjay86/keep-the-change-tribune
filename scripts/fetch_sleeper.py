"""
fetch_sleeper.py — pull league data from the public Sleeper API.

Usage: python fetch_sleeper.py [--out data.json]
Writes JSON bundle with league, users, rosters, matchups, and nfl_state.
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://api.sleeper.app/v1"

def _get(path: str):
    url = f"{BASE}{path}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())

def fetch(league_id: str) -> dict:
    league = _get(f"/league/{league_id}")
    users = _get(f"/league/{league_id}/users")
    rosters = _get(f"/league/{league_id}/rosters")

    # Current week + season status from the NFL state endpoint
    nfl_state = _get("/state/nfl")
    week = nfl_state.get("week", 1)

    matchups = _get(f"/league/{league_id}/matchups/{week}")

    return {
        "league": league,
        "users": users,
        "rosters": rosters,
        "matchups": matchups,
        "nfl_state": nfl_state,
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