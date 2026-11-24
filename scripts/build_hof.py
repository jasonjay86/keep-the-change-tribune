"""
build_hof.py — The Keep The Change Hall of Fame.

Walks the whole Sleeper league chain (2019 -> present), computes a full
all-time record book, and renders hof.html in the Tribune's newsprint style.

Usage:
  python scripts/build_hof.py [--refresh] [--out hof.html]

Data is cached under hof_cache/ (gitignored) so repeat runs are cheap.
--refresh ignores the cache for season-level payloads (leagues, rosters,
matchups, transactions) but always re-reads the in-progress season.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE = "https://api.sleeper.app/v1"
CURRENT_LEAGUE = "1312862141639839744"
CACHE = Path("hof_cache")
STATS_CACHE = CACHE / "player_stats"

# ---------------------------------------------------------------- fetch layer

def _get(path: str, cache_key: str | None = None, refresh: bool = False):
    """GET with an optional on-disk cache. cache_key relative to CACHE."""
    if cache_key:
        fp = CACHE / f"{cache_key}.json"
        if fp.exists() and not refresh:
            try:
                return json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
    url = f"{BASE}{path}"
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ktc-hof/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            data = json.loads(raw)
            if cache_key:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(json.dumps(data), encoding="utf-8")
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 * (attempt + 1))
    print(f"[hof] WARN fetch failed {path}: {last}", file=sys.stderr)
    return None


def league_chain(refresh: bool) -> list[dict]:
    """Walk previous_league_id back to the league's first season."""
    chain, cur, seen = [], CURRENT_LEAGUE, set()
    while cur and cur not in seen:
        seen.add(cur)
        lg = _get(f"/league/{cur}", f"league_{cur}", refresh)
        if not lg:
            break
        chain.append(lg)
        cur = lg.get("previous_league_id")
    return list(reversed(chain))  # oldest first


# ---------------------------------------------------------------- season load

def load_season(lg: dict, refresh: bool) -> dict:
    lid = lg["league_id"]
    season = str(lg.get("season"))
    settings = lg.get("settings") or {}
    playoff_start = int(settings.get("playoff_week_start") or 15)
    total_weeks = int(settings.get("playoff_week_start") or 15) + 2

    rosters = _get(f"/league/{lid}/rosters", f"rosters_{lid}", refresh) or []
    users = _get(f"/league/{lid}/users", f"users_{lid}", refresh) or []
    winners = _get(f"/league/{lid}/winners_bracket", f"wb_{lid}", refresh) or []
    drafts = _get(f"/league/{lid}/drafts", f"drafts_{lid}", refresh) or []

    matchups = {}
    for wk in range(1, total_weeks + 1):
        rows = _get(f"/league/{lid}/matchups/{wk}", f"mu_{lid}_{wk}", refresh) or []
        if rows:
            matchups[wk] = rows

    transactions = {}
    for wk in range(1, total_weeks + 1):
        rows = _get(f"/league/{lid}/transactions/{wk}", f"tx_{lid}_{wk}", refresh) or []
        if rows:
            transactions[wk] = rows

    picks = []
    for d in drafts:
        did = d.get("draft_id")
        if not did:
            continue
        p = _get(f"/draft/{did}/picks", f"picks_{did}", refresh) or []
        picks.extend(p)

    return {
        "league_id": lid,
        "season": season,
        "status": lg.get("status"),
        "name": lg.get("name"),
        "playoff_start": playoff_start,
        "regular_weeks": playoff_start - 1,
        "rosters": rosters,
        "users": users,
        "winners": winners,
        "drafts": drafts,
        "picks": picks,
        "matchups": matchups,
        "transactions": transactions,
        "scoring": lg.get("scoring_settings") or {},
        "total_rosters": lg.get("total_rosters") or len(rosters),
    }


def player_week_points(season: str, weeks: list[int], player_ids: set[str], refresh: bool) -> dict[str, float]:
    """Total half-PPR points per player for the given weeks (cached, slim)."""
    STATS_CACHE.mkdir(parents=True, exist_ok=True)
    totals: dict[str, float] = {}
    for wk in weeks:
        fp = STATS_CACHE / f"{season}_{wk}.json"
        slim = None
        if fp.exists() and not refresh:
            try:
                slim = json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                slim = None
        if slim is None:
            raw = _get(f"/stats/nfl/regular/{season}/{wk}")
            if not raw:
                continue
            slim = {}
            for pid, st in raw.items():
                if pid not in player_ids or not isinstance(st, dict):
                    continue
                pts = st.get("pts_half_ppr")
                if isinstance(pts, (int, float)) and pts > 0:
                    slim[pid] = round(float(pts), 2)
            fp.write_text(json.dumps(slim), encoding="utf-8")
        for pid, pts in slim.items():
            totals[pid] = totals.get(pid, 0.0) + float(pts)
    return totals


# ---------------------------------------------------------------- compute

def fmt(n, nd=1):
    return f"{n:.{nd}f}" if isinstance(n, (int, float)) else "—"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def letter_grade(z: float) -> str:
    # z = standard deviations from the league's average draft haul
    scale = [
        (1.55, "A+"), (1.15, "A"), (0.85, "A-"), (0.55, "B+"), (0.25, "B"),
        (-0.05, "B-"), (-0.35, "C+"), (-0.65, "C"), (-0.95, "C-"),
        (-1.25, "D+"), (-1.55, "D"), (-2.0, "D-"),
    ]
    for cut, g in scale:
        if z >= cut:
            return g
    return "F"


def compute(seasons_raw: list[dict], refresh: bool) -> dict:
    managers: dict[str, dict] = {}
    seasons_out = []
    games: list[dict] = []           # every completed matchup game
    weekly_scores: list[dict] = []   # every team-week score
    season_totals: list[dict] = []   # per team-season aggregates
    tx_rows: list[dict] = []
    draft_teams: list[dict] = []
    picks_out: list[dict] = []
    seed_of: dict[tuple, int] = {}
    bracket_by_season: dict[str, set] = {}

    for s in seasons_raw:
        season = s["season"]
        lid = s["league_id"]
        reg_weeks = s["regular_weeks"]

        # ---- identity maps
        user_by_id = {u.get("user_id"): u for u in s["users"]}
        roster_owner: dict[int, str] = {}
        team_names: dict[int, str] = {}
        for r in s["rosters"]:
            oid = r.get("owner_id")
            rid = r.get("roster_id")
            roster_owner[rid] = oid
            md = (user_by_id.get(oid) or {}).get("metadata") or {}
            team_names[rid] = (md.get("team_name") or "").strip()

        def mkey(rid):
            oid = roster_owner.get(rid)
            u = user_by_id.get(oid) or {}
            return (u.get("display_name") or u.get("username") or f"roster{rid}")

        # ---- per-team-season accumulators
        ts = {}
        for r in s["rosters"]:
            rid = r["roster_id"]
            st = r.get("settings") or {}
            ts[rid] = {
                "season": season, "roster_id": rid, "manager": mkey(rid),
                "team": team_names.get(rid) or "", "w": 0, "l": 0, "t": 0,
                "pf": 0.0, "pa": 0.0, "weeks": 0, "playoff_w": 0, "playoff_l": 0,
            }

        # ---- walk weeks
        for wk in sorted(s["matchups"]):
            rows = s["matchups"][wk]
            by_match: dict[int, list] = {}
            for row in rows:
                mid = row.get("matchup_id")
                by_match.setdefault(mid, []).append(row)
                pts = row.get("points")
                rid = row.get("roster_id")
                if isinstance(pts, (int, float)) and pts > 0 and rid in ts:
                    weekly_scores.append({"season": season, "week": wk, "manager": ts[rid]["manager"],
                                          "team": ts[rid]["team"], "points": float(pts),
                                          "playoff": wk >= s["playoff_start"]})
                    ts[rid]["pf"] += float(pts)
                    ts[rid]["weeks"] += 1
            for mid, pair in by_match.items():
                if len(pair) != 2:
                    continue
                a, b = pair
                pa, pb = a.get("points"), b.get("points")
                if not (isinstance(pa, (int, float)) and isinstance(pb, (int, float))):
                    continue
                if pa <= 0 or pb <= 0:
                    continue
                ra, rb = a.get("roster_id"), b.get("roster_id")
                if ra not in ts or rb not in ts:
                    continue
                playoff = wk >= s["playoff_start"]
                ts[ra]["pa"] += float(pb)
                ts[rb]["pa"] += float(pa)
                g = {"season": season, "week": wk, "playoff": playoff,
                     "a": ts[ra]["manager"], "a_team": ts[ra]["team"], "a_pts": float(pa),
                     "b": ts[rb]["manager"], "b_team": ts[rb]["team"], "b_pts": float(pb)}
                games.append(g)
                if playoff:
                    if pa > pb:
                        ts[ra]["playoff_w"] += 1; ts[rb]["playoff_l"] += 1
                    elif pb > pa:
                        ts[rb]["playoff_w"] += 1; ts[ra]["playoff_l"] += 1
                else:
                    for me, mine, theirs in ((ra, pa, pb), (rb, pb, pa)):
                        if mine > theirs:
                            ts[me]["w"] += 1
                        elif mine < theirs:
                            ts[me]["l"] += 1
                        else:
                            ts[me]["t"] += 1

        # ---- playoff finale from bracket
        champ_rid = runner_rid = None
        third_rid = None
        bracket_rids = set()
        for g in s["winners"]:
            for k in ("t1", "t2"):
                if isinstance(g.get(k), int):
                    bracket_rids.add(g[k])
            if g.get("p") == 1:
                champ_rid = g.get("w"); runner_rid = g.get("l")
            if g.get("p") == 3:
                third_rid = g.get("w")

        def champ_final(winner_mgr, loser_mgr):
            for wk in sorted(s["matchups"], reverse=True):
                if wk < s["playoff_start"]:
                    continue
                groups: dict[int, list] = {}
                for row in s["matchups"][wk]:
                    groups.setdefault(row.get("matchup_id"), []).append(row)
                for pair in groups.values():
                    if len(pair) != 2:
                        continue
                    mgrs = {ts[pair[0]["roster_id"]]["manager"], ts[pair[1]["roster_id"]]["manager"]}
                    if mgrs == {winner_mgr, loser_mgr}:
                        p0, p1 = pair[0].get("points") or 0, pair[1].get("points") or 0
                        hi, lo = max(p0, p1), min(p0, p1)
                        return f"{hi:.1f}–{lo:.1f}"
            return ""

        # ---- season standings by regular-season record then PF
        standings = sorted(
            ts.values(),
            key=lambda x: (-(x["w"] - x["l"]), -x["pf"]),
        )
        for i, row in enumerate(standings, 1):
            row["seed"] = i
            row["finish"] = i

        champ = ts.get(champ_rid) if champ_rid is not None else None
        runner = ts.get(runner_rid) if runner_rid is not None else None
        if champ:
            champ["finish"] = 1
        if runner:
            runner["finish"] = 2
        if third_rid is not None and third_rid in ts:
            ts[third_rid]["finish"] = 3

        # ---- transactions
        for wk, rows in s["transactions"].items():
            for t in rows:
                typ = t.get("type")
                rids = t.get("roster_ids") or []
                for rid in rids:
                    if rid not in ts:
                        continue
                    tx_rows.append({"season": season, "week": wk, "type": typ,
                                    "manager": ts[rid]["manager"]})

        # ---- drafts (grades computed after player points are known)
        draft_teams.extend([])  # placeholder, filled below
        picks_by_draft: dict[str, list] = {}
        for p in s["picks"]:
            picks_by_draft.setdefault(p.get("draft_id") or "?", []).append(p)

        for did, plist in picks_by_draft.items():
            drafted_ids = {str(p.get("player_id")) for p in plist if p.get("player_id")}
            weeks_to_pull = [w for w in range(1, 19) if w <= max(s["matchups"].keys() or [0])]
            pts = player_week_points(season, weeks_to_pull, drafted_ids, refresh) if drafted_ids else {}
            rows = []
            for p in plist:
                pid = str(p.get("player_id") or "")
                md = p.get("metadata") or {}
                nm = f"{md.get('first_name','')} {md.get('last_name','')}".strip()
                rid = p.get("roster_id")
                rows.append({
                    "pick_no": p.get("pick_no"), "round": p.get("round"),
                    "roster_id": rid, "manager": ts[rid]["manager"] if rid in ts else "?",
                    "player": nm or "—", "position": md.get("position") or "",
                    "team": md.get("team") or "", "player_id": pid,
                    "points": round(pts.get(pid, 0.0), 2),
                })
            by_round: dict[int, list] = {}
            for r in rows:
                by_round.setdefault(r["round"] or 0, []).append(r["points"])
            rnd_mean = {k: statistics.mean(v) for k, v in by_round.items() if v}
            for r in rows:
                r["expected"] = rnd_mean.get(r["round"] or 0, 0.0)
                r["surplus"] = r["points"] - r["expected"]
            by_team: dict[str, float] = {}
            for r in rows:
                by_team[r["manager"]] = by_team.get(r["manager"], 0.0) + r["surplus"]
            vals = list(by_team.values())
            mu = statistics.mean(vals) if vals else 0
            sd = statistics.pstdev(vals) if len(vals) > 1 else 1
            sd = sd or 1
            for mgr, v in by_team.items():
                mine = [r for r in rows if r["manager"] == mgr]
                best = max(mine, key=lambda r: r["points"]) if mine else None
                worst = min(mine, key=lambda r: r["surplus"]) if mine else None
                draft_teams.append({
                    "season": season, "manager": mgr, "surplus": round(v, 1),
                    "grade": letter_grade((v - mu) / sd),
                    "picks": len(mine),
                    "best": best["player"] if best else "—",
                    "best_pts": best["points"] if best else 0,
                    "worst": worst["player"] if worst else "—",
                    "worst_pts": worst["points"] if worst else 0,
                })
            picks_out.extend([{**r, "season": season} for r in rows])

        seasons_out.append({
            "season": season,
            "league_id": lid,
            "in_progress": s["status"] != "complete",
            "champion": champ["manager"] if champ else None,
            "champion_team": (champ or {}).get("team") or "",
            "champion_record": f"{champ['w']}-{champ['l']}" if champ else "",
            "runner_up": runner["manager"] if runner else None,
            "runner_up_team": (runner or {}).get("team") or "",
            "runner_up_record": f"{runner['w']}-{runner['l']}" if runner else "",
            "top_seed": standings[0]["manager"] if standings else None,
            "top_seed_record": f"{standings[0]['w']}-{standings[0]['l']}" if standings else "",
            "teams": len(ts),
            "status": s["status"],
            "final_score": champ_final(champ["manager"], runner["manager"]) if (champ and runner) else "",
            "weeks_played": max([w for w in s["matchups"] if any((r.get("points") or 0) > 0 for r in s["matchups"][w])] or [0]),
        })

        # remember seeds for upset detection and bracket membership for playoff apps
        for rid, row in ts.items():
            seed_of[(season, row["manager"])] = row.get("seed", 99)
        for rid in bracket_rids:
            if rid in ts:
                bracket_by_season.setdefault(ts[rid]["manager"], set()).add(season)


        season_totals.extend(ts.values())

    # ---------- manager roll-up
    def m(mgr):
        return managers.setdefault(mgr, {
            "name": mgr, "team": "", "seasons": set(), "w": 0, "l": 0, "t": 0,
            "pf": 0.0, "pa": 0.0, "weeks": 0, "titles": [], "runner_ups": [],
            "playoff_apps": 0, "finishes": [], "weekly_highs": 0,
            "best_score": 0.0, "worst_score": 9999.0, "best_win": 0.0, "worst_loss": 0.0,
            "trades": 0, "waivers": 0, "fa": 0, "draft_surplus": 0.0, "draft_grades": [],
        })

    for row in season_totals:
        d = m(row["manager"])
        d["seasons"].add(row["season"])
        if row["team"]:
            d["team"] = row["team"]
        d["w"] += row["w"]; d["l"] += row["l"]; d["t"] += row["t"]
        d["pf"] += row["pf"]; d["pa"] += row["pa"]; d["weeks"] += row["weeks"]
        d["finishes"].append((row["season"], row.get("finish", 99)))
        d["pw"] = d.get("pw", 0) + row.get("playoff_w", 0)
        d["pl"] = d.get("pl", 0) + row.get("playoff_l", 0)

    for mgr, season_set in bracket_by_season.items():
        m(mgr)["playoff_apps"] = len(season_set)

    for srow in seasons_out:
        if srow["champion"]:
            m(srow["champion"])["titles"].append(srow["season"])
        if srow["runner_up"]:
            m(srow["runner_up"])["runner_ups"].append(srow["season"])

    for sc in weekly_scores:
        d = m(sc["manager"])
        if not sc["playoff"]:
            d["best_score"] = max(d["best_score"], sc["points"])
            d["worst_score"] = min(d["worst_score"], sc["points"])

    # weekly high scores (top score across the league in each completed week)
    weeks_grouped: dict[tuple, list] = {}
    for sc in weekly_scores:
        if not sc["playoff"]:
            weeks_grouped.setdefault((sc["season"], sc["week"]), []).append(sc)
    for k, group in weeks_grouped.items():
        top = max(group, key=lambda x: x["points"])
        m(top["manager"])["weekly_highs"] += 1

    # margins
    for g in games:
        if g["playoff"]:
            continue
        for me, mine, theirs in ((g["a"], g["a_pts"], g["b_pts"]), (g["b"], g["b_pts"], g["a_pts"])):
            d = m(me)
            if mine > theirs:
                d["best_win"] = max(d["best_win"], mine - theirs)
            elif mine < theirs:
                d["worst_loss"] = max(d["worst_loss"], theirs - mine)

    for t in tx_rows:
        d = m(t["manager"])
        if t["type"] == "trade":
            d["trades"] += 1
        elif t["type"] == "waiver":
            d["waivers"] += 1
        elif t["type"] == "free_agent":
            d["fa"] += 1

    for dt in draft_teams:
        d = m(dt["manager"])
        d["draft_surplus"] += dt["surplus"]
        d["draft_grades"].append(dt["grade"])

    # streaks from ordered game log
    game_log: dict[str, list] = {}
    for g in games:
        if g["playoff"]:
            continue
        game_log.setdefault(g["a"], []).append((g["season"], g["week"], 1 if g["a_pts"] > g["b_pts"] else (-1 if g["a_pts"] < g["b_pts"] else 0)))
        game_log.setdefault(g["b"], []).append((g["season"], g["week"], 1 if g["b_pts"] > g["a_pts"] else (-1 if g["b_pts"] < g["a_pts"] else 0)))
    for mgr, log in game_log.items():
        log.sort()
        best_w = best_l = cur_w = cur_l = 0
        for _, _, res in log:
            if res == 1:
                cur_w += 1; cur_l = 0
            elif res == -1:
                cur_l += 1; cur_w = 0
            else:
                cur_w = cur_l = 0
            best_w = max(best_w, cur_w); best_l = max(best_l, cur_l)
        managers[mgr]["streak_w"] = best_w
        managers[mgr]["streak_l"] = best_l

    # all-play luck
    allplay: dict[str, float] = {}
    for k, group in weeks_grouped.items():
        n = len(group)
        if n < 2:
            continue
        for t in group:
            wins = sum(1 for o in group if o is not t and t["points"] > o["points"])
            ties = sum(1 for o in group if o is not t and t["points"] == o["points"])
            allplay[t["manager"]] = allplay.get(t["manager"], 0.0) + (wins + 0.5 * ties) / (n - 1)

    # ---------- finalise manager rows
    mrows = []
    for name, d in managers.items():
        g = d["w"] + d["l"] + d["t"]
        exp = allplay.get(name, 0.0)
        best_fin = min([f for _, f in d["finishes"]], default=None)
        worst_fin = max([f for _, f in d["finishes"]], default=None)
        mrows.append({
            "name": name, "team": d["team"] or name,
            "seasons": len(d["seasons"]),
            "w": d["w"], "l": d["l"], "t": d["t"],
            "record": f"{d['w']}-{d['l']}" + (f"-{d['t']}" if d["t"] else ""),
            "pct": (d["w"] + 0.5 * d["t"]) / g if g else 0.0,
            "pf": round(d["pf"], 1), "pa": round(d["pa"], 1),
            "ppg": round(d["pf"] / d["weeks"], 1) if d["weeks"] else 0.0,
            "papg": round(d["pa"] / d["weeks"], 1) if d["weeks"] else 0.0,
            "diff": round(d["pf"] - d["pa"], 1),
            "titles": d["titles"], "runner_ups": d["runner_ups"],
            "playoff_apps": d["playoff_apps"],
            "playoff_record": f"{d.get('pw',0)}-{d.get('pl',0)}",
            "best_finish": best_fin, "worst_finish": worst_fin,
            "streak_w": d.get("streak_w", 0), "streak_l": d.get("streak_l", 0),
            "best_score": round(d["best_score"], 1),
            "worst_score": round(d["worst_score"], 1) if d["worst_score"] < 9999 else 0.0,
            "best_win": round(d["best_win"], 1), "worst_loss": round(d["worst_loss"], 1),
            "weekly_highs": d["weekly_highs"],
            "trades": d["trades"], "waivers": d["waivers"], "fa": d["fa"],
            "moves": d["trades"] + d["waivers"] + d["fa"],
            "draft_surplus": round(d["draft_surplus"], 1),
            "luck": round((d["w"] + 0.5 * d["t"]) - exp, 2),
            "games": g,
        })
    mrows.sort(key=lambda x: (-len(x["titles"]), -x["w"], -x["pct"]))

    # ---------- league records
    best_sc = max(weekly_scores, key=lambda x: x["points"])
    worst_sc = min(weekly_scores, key=lambda x: x["points"])
    decisives = [g for g in games if g["a_pts"] != g["b_pts"]]
    blow = max(decisives, key=lambda g: abs(g["a_pts"] - g["b_pts"]))
    close = min(decisives, key=lambda g: abs(g["a_pts"] - g["b_pts"]))
    high_total = max(games, key=lambda g: g["a_pts"] + g["b_pts"])
    low_total = min(games, key=lambda g: g["a_pts"] + g["b_pts"])

    # most points in a loss / fewest in a win
    mp_loss = max(decisives, key=lambda g: min(g["a_pts"], g["b_pts"]))
    fp_win = min(decisives, key=lambda g: max(g["a_pts"], g["b_pts"]))

    def side(g, winner=True):
        hi = g["a_pts"] > g["b_pts"]
        me = g["a"] if (hi == winner) else g["b"]
        mt = g["a_team"] if (hi == winner) else g["b_team"]
        pts = max(g["a_pts"], g["b_pts"]) if winner else min(g["a_pts"], g["b_pts"])
        op = g["b"] if (hi == winner) else g["a"]
        opp = min(g["a_pts"], g["b_pts"]) if winner else max(g["a_pts"], g["b_pts"])
        return me, mt, pts, op, opp

    # season-level extremes (completed seasons only)
    complete = [r for r in season_totals if str(r["season"]) != seasons_out[-1]["season"]]
    pool = complete or season_totals
    best_season = max(pool, key=lambda r: r["pf"])
    worst_season = min(pool, key=lambda r: r["pf"])
    best_record = max(pool, key=lambda r: (r["w"] - r["l"]))
    worst_record = min(pool, key=lambda r: (r["w"] - r["l"]))

    # playoff upsets (winner was seeded worse than the loser)
    upsets = []
    for g in games:
        if not g["playoff"] or g["a_pts"] == g["b_pts"]:
            continue
        win_is_a = g["a_pts"] > g["b_pts"]
        wm, lm = (g["a"], g["b"]) if win_is_a else (g["b"], g["a"])
        ws = seed_of.get((g["season"], wm), 99)
        ls = seed_of.get((g["season"], lm), 99)
        if ls < ws and ls < 99:
            upsets.append({"season": g["season"], "week": g["week"], "winner": wm, "loser": lm,
                           "winner_pt": max(g["a_pts"], g["b_pts"]), "loser_pt": min(g["a_pts"], g["b_pts"]),
                           "gap": ws - ls, "ws": ws, "ls": ls})
    biggest_upset = max(upsets, key=lambda u: (u["gap"], -(u["loser_pt"] - u["winner_pt"]))) if upsets else None

    # ---------- rivalries
    pairs: dict[tuple, list] = {}
    for g in games:
        if g["playoff"]:
            continue
        k = tuple(sorted([g["a"], g["b"]]))
        pairs.setdefault(k, []).append(g)
    rivalries = []
    for (x, y), gs in pairs.items():
        gs.sort(key=lambda g: (g["season"], g["week"]))
        xw = sum(1 for g in gs if (g["a"] == x and g["a_pts"] > g["b_pts"]) or (g["b"] == x and g["b_pts"] > g["a_pts"]))
        yw = len(gs) - xw - sum(1 for g in gs if g["a_pts"] == g["b_pts"])
        ties = len(gs) - xw - yw
        closest = min(gs, key=lambda g: abs(g["a_pts"] - g["b_pts"]))
        biggest = max(gs, key=lambda g: abs(g["a_pts"] - g["b_pts"]))
        # current streak
        streak_holder, streak_len = None, 0
        for g in reversed(gs):
            if g["a_pts"] == g["b_pts"]:
                break
            win = g["a"] if g["a_pts"] > g["b_pts"] else g["b"]
            if streak_holder is None:
                streak_holder, streak_len = win, 1
            elif win == streak_holder:
                streak_len += 1
            else:
                break
        rivalries.append({
            "a": x, "b": y, "games": len(gs), "a_wins": xw, "b_wins": yw, "ties": ties,
            "avg_margin": round(statistics.mean(abs(g["a_pts"] - g["b_pts"]) for g in gs), 2),
            "closest": f"{closest['season']} W{closest['week']} · {abs(closest['a_pts']-closest['b_pts']):.1f}",
            "biggest": f"{biggest['season']} W{biggest['week']} · {abs(biggest['a_pts']-biggest['b_pts']):.1f}",
            "streak": f"{streak_holder} · {streak_len}" if streak_holder else "—",
            "last": (f"{gs[-1]['season']} W{gs[-1]['week']}: "
                     f"{gs[-1]['a_pts']:.1f}–{gs[-1]['b_pts']:.1f} ({gs[-1]['a']})") if gs else "",
        })
    rivalries.sort(key=lambda r: -r["games"])

    records = [
        {"label": "Highest Weekly Score", "value": f"{best_sc['points']:.1f}", "holder": best_sc["manager"],
         "detail": f"{best_sc['season']} · Week {best_sc['week']}"},
        {"label": "Lowest Weekly Score", "value": f"{worst_sc['points']:.1f}", "holder": worst_sc["manager"],
         "detail": f"{worst_sc['season']} · Week {worst_sc['week']}"},
        {"label": "Biggest Blowout", "value": f"{abs(blow['a_pts']-blow['b_pts']):.1f}", "holder": side(blow)[0],
         "detail": f"{blow['season']} · Week {blow['week']} · {max(blow['a_pts'],blow['b_pts']):.1f}–{min(blow['a_pts'],blow['b_pts']):.1f}"},
        {"label": "Closest Finish", "value": f"{abs(close['a_pts']-close['b_pts']):.2f}", "holder": side(close)[0],
         "detail": f"{close['season']} · Week {close['week']} · {max(close['a_pts'],close['b_pts']):.1f}–{min(close['a_pts'],close['b_pts']):.1f}"},
        {"label": "Highest-Scoring Game", "value": f"{high_total['a_pts']+high_total['b_pts']:.1f}", "holder": f"{high_total['a']} v {high_total['b']}",
         "detail": f"{high_total['season']} · Week {high_total['week']} · {high_total['a_pts']:.1f}–{high_total['b_pts']:.1f}"},
        {"label": "Lowest-Scoring Game", "value": f"{low_total['a_pts']+low_total['b_pts']:.1f}", "holder": f"{low_total['a']} v {low_total['b']}",
         "detail": f"{low_total['season']} · Week {low_total['week']} · {low_total['a_pts']:.1f}–{low_total['b_pts']:.1f}"},
        {"label": "Most Points In A Loss", "value": f"{side(mp_loss, winner=False)[2]:.1f}", "holder": side(mp_loss, winner=False)[0],
         "detail": f"{mp_loss['season']} · Week {mp_loss['week']} · lost to {side(mp_loss, winner=False)[3]}"},
        {"label": "Fewest Points In A Win", "value": f"{side(fp_win)[2]:.1f}", "holder": side(fp_win)[0],
         "detail": f"{fp_win['season']} · Week {fp_win['week']} · beat {side(fp_win)[3]}"},
        {"label": "Most Points, One Season", "value": f"{best_season['pf']:.1f}", "holder": best_season["manager"],
         "detail": f"{best_season['season']} · {best_season['w']}-{best_season['l']}"},
        {"label": "Fewest Points, One Season", "value": f"{worst_season['pf']:.1f}", "holder": worst_season["manager"],
         "detail": f"{worst_season['season']} · {worst_season['w']}-{worst_season['l']}"},
        {"label": "Best Regular Season", "value": f"{best_record['w']}-{best_record['l']}", "holder": best_record["manager"],
         "detail": f"{best_record['season']} · {best_record['pf']:.1f} PF"},
        {"label": "Worst Regular Season", "value": f"{worst_record['w']}-{worst_record['l']}", "holder": worst_record["manager"],
         "detail": f"{worst_record['season']} · {worst_record['pf']:.1f} PF"},
        {"label": "Longest Winning Streak", "value": f"{max(mrows, key=lambda x: x['streak_w'])['streak_w']}", "holder": max(mrows, key=lambda x: x['streak_w'])["name"],
         "detail": "Regular season, all-time"},
        {"label": "Longest Losing Streak", "value": f"{max(mrows, key=lambda x: x['streak_l'])['streak_l']}", "holder": max(mrows, key=lambda x: x['streak_l'])["name"],
         "detail": "Regular season, all-time"},
        {"label": "Most Weekly High Scores", "value": f"{max(mrows, key=lambda x: x['weekly_highs'])['weekly_highs']}", "holder": max(mrows, key=lambda x: x['weekly_highs'])["name"],
         "detail": "Top score in the league that week"},
    ]
    if biggest_upset:
        records.append({"label": "Biggest Playoff Upset", "value": f"#{biggest_upset['ws']} over #{biggest_upset['ls']}",
                        "holder": biggest_upset["winner"],
                        "detail": f"{biggest_upset['season']} · W{biggest_upset['week']} · beat {biggest_upset['loser']} "
                                  f"{biggest_upset['winner_pt']:.1f}–{biggest_upset['loser_pt']:.1f}"})

    # best/worst single draft picks
    picks_with_pts = [p for p in picks_out if p["points"] > 0]
    best_pick = max(picks_with_pts, key=lambda p: p["surplus"]) if picks_with_pts else None
    worst_pick = min(picks_with_pts, key=lambda p: p["surplus"]) if picks_with_pts else None
    top_scorer_pick = max(picks_with_pts, key=lambda p: p["points"]) if picks_with_pts else None

    # ---------- draft tables grouped by season
    by_season: dict[str, list] = {}
    for d in draft_teams:
        by_season.setdefault(d["season"], []).append(d)
    drafts_by_season = []
    for season in sorted(by_season, reverse=True):
        teams = sorted(by_season[season], key=lambda d: -d["surplus"])
        for i, t in enumerate(teams, 1):
            t["rank"] = i
        drafts_by_season.append({"season": season, "teams": teams})

    # ---------- head-to-head matrix (regular season)
    h2h_managers = [r["name"] for r in mrows]
    cells: dict[str, dict[str, str]] = {n: {} for n in h2h_managers}
    for r in rivalries:
        a, b = r["a"], r["b"]
        if a in cells and b in cells:
            cells[a][b] = f"{r['a_wins']}-{r['b_wins']}" + (f"-{r['ties']}" if r["ties"] else "")
            cells[b][a] = f"{r['b_wins']}-{r['a_wins']}" + (f"-{r['ties']}" if r["ties"] else "")

    # ---------- reigning champion
    reigning = next((s["champion"] for s in reversed(seasons_out) if s.get("champion")), None)

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "league_name": "Keep The Change",
        "reigning_champion": reigning,
        "seasons": seasons_out,
        "managers": mrows,
        "records": records,
        "rivalries": rivalries,
        "h2h_managers": h2h_managers,
        "h2h_cells": cells,
        "drafts": sorted(draft_teams, key=lambda d: (d["season"], -d["surplus"])),
        "drafts_by_season": drafts_by_season,
        "draft_highlights": {
            "best_pick": best_pick, "worst_pick": worst_pick, "top_scorer_pick": top_scorer_pick,
        },
        "totals": {
            "seasons": len(seasons_out),
            "managers": len(mrows),
            "games": len(games),
            "weeks": len(weeks_grouped),
            "first_season": seasons_out[0]["season"] if seasons_out else "",
            "last_season": seasons_out[-1]["season"] if seasons_out else "",
            "trades": sum(1 for t in tx_rows if t["type"] == "trade"),
            "moves": len(tx_rows),
        },
    }


def render(stats: dict, out: str, template_path: str) -> None:
    env = Environment(loader=FileSystemLoader(str(Path(template_path).parent)),
                      autoescape=select_autoescape(["html"]))
    env.filters["capitalize"] = lambda s: (s or "").capitalize()
    html = env.get_template(Path(template_path).name).render(**stats)
    Path(out).write_text(html, encoding="utf-8")
    print(f"[hof] wrote {out} ({len(html)} bytes)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="ignore season-level cache")
    ap.add_argument("--out-json", default="hof.json")
    ap.add_argument("--out", default="hof.html")
    ap.add_argument("--template", default="templates/hof.html.j2")
    ap.add_argument("--data-only", action="store_true")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    chain = league_chain(args.refresh)
    print(f"[hof] seasons found: {[l['season'] for l in chain]}")
    raw = []
    for lg in chain:
        s = load_season(lg, args.refresh)
        played = sum(1 for r in s["matchups"].values() for x in r if (x.get("points") or 0) > 0)
        print(f"[hof] {s['season']}: {len(s['rosters'])} teams · {len(s['matchups'])} weeks · {played} scored rows · {len(s['picks'])} picks")
        raw.append(s)

    stats = compute(raw, args.refresh)
    Path(args.out_json).write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print(f"[hof] wrote {args.out_json}")
    if not args.data_only:
        render(stats, args.out, args.template)


if __name__ == "__main__":
    main()
