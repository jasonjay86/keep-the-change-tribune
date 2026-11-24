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


def nfl_state() -> dict:
    """Current NFL state — used to detect the live (partially scored) week."""
    return _get("/state/nfl") or {}


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

def load_season(lg: dict, refresh: bool, live_week: int | None = None) -> dict:
    lid = lg["league_id"]
    season = str(lg.get("season"))
    settings = lg.get("settings") or {}
    playoff_start = int(settings.get("playoff_week_start") or 15)
    total_weeks = int(settings.get("playoff_week_start") or 15) + 2

    rosters = _get(f"/league/{lid}/rosters", f"rosters_{lid}", refresh) or []
    users = _get(f"/league/{lid}/users", f"users_{lid}", refresh) or []
    winners = _get(f"/league/{lid}/winners_bracket", f"wb_{lid}", refresh) or []

    # Recent weeks are volatile: Sleeper keeps applying stat corrections to a
    # week after it "ends", so a payload cached mid-game freezes a wrong score
    # forever (a live week 11 once logged 105.58-104.58 for a game that finished
    # 127.68-111.08). Always refetch the live week and the two behind it.
    volatile_from = max(1, live_week - 2) if live_week else None

    def _volatile(wk: int) -> bool:
        return bool(volatile_from and wk >= volatile_from)

    matchups = {}
    for wk in range(1, total_weeks + 1):
        rows = _get(f"/league/{lid}/matchups/{wk}", f"mu_{lid}_{wk}",
                    refresh or _volatile(wk)) or []
        if rows:
            matchups[wk] = rows

    transactions = {}
    for wk in range(1, total_weeks + 1):
        rows = _get(f"/league/{lid}/transactions/{wk}", f"tx_{lid}_{wk}",
                    refresh or _volatile(wk)) or []
        if rows:
            transactions[wk] = rows

    # Drafts exist only to value traded picks: a pick is worth whatever the
    # player taken with it did. Completed drafts never change, so they cache
    # forever; a season whose draft has not run yet simply returns nothing.
    #
    # Sleeper records a traded pick under the roster that USED it, not the slot
    # it came from, so the only reliable join is the slot: draft_order maps
    # user -> slot at creation, and within a linear draft the Nth pick of a
    # round is slot N.
    draft_slots: dict[tuple, str] = {}   # (season, round, slot) -> player_id
    draft_order: dict[str, int] = {}     # user_id -> slot
    for d in _get(f"/league/{lid}/drafts", f"drafts_{lid}", refresh) or []:
        did = d.get("draft_id")
        if not did or d.get("status") not in ("complete", "drafting", None):
            continue
        meta = _get(f"/draft/{did}", f"draft_{did}", refresh) or {}
        for uid, slot in (meta.get("draft_order") or d.get("draft_order") or {}).items():
            draft_order[str(uid)] = int(slot)
        by_round: dict[int, list] = {}
        for pk in _get(f"/draft/{did}/picks", f"picks_{did}", refresh) or []:
            if pk.get("round") is not None:
                by_round.setdefault(int(pk["round"]), []).append(pk)
        for rnd, plist in by_round.items():
            plist.sort(key=lambda x: x.get("pick_no") or 0)
            for i, pk in enumerate(plist, 1):
                if pk.get("player_id"):
                    draft_slots[(str(meta.get("season") or season), rnd, i)] = pk["player_id"]

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
        "matchups": matchups,
        "transactions": transactions,
        "draft_slots": draft_slots,
        "draft_order": draft_order,
        "scoring": lg.get("scoring_settings") or {},
        "live_week": live_week,
        "total_rosters": lg.get("total_rosters") or len(rosters),
    }


def player_names() -> dict:
    """{player_id: [name, position]} from a slim cache of the NFL players DB."""
    slim = CACHE / "player_names.json"
    if slim.exists():
        try:
            return json.loads(slim.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    src = Path("cache/players_nfl.json")
    if src.exists():
        db = json.loads(src.read_text(encoding="utf-8"))
    else:
        db = _get("/players/nfl") or {}
    out = {}
    for pid, pl in db.items():
        if not isinstance(pl, dict):
            continue
        name = f"{pl.get('first_name') or ''} {pl.get('last_name') or ''}".strip()
        if name:
            out[pid] = [name, pl.get("position") or ""]
    CACHE.mkdir(parents=True, exist_ok=True)
    slim.write_text(json.dumps(out), encoding="utf-8")
    return out


# ---------------------------------------------------------------- commentary

GRADE_MIN_WEEKS = 3   # below this a trade is a snapshot, not a verdict

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-flash"      # DeepSeek-V4.1-Flash
BLURB_CACHE = CACHE / "history_blurbs.json"

BLURB_SYSTEM = """You write one-line career notes for a vintage football newspaper's Hall of
Fame, in the voice of Howard Cosell: brash, staccato, declarative, theatrical, forever
delivering a verdict. Think "I'm telling you," "let me tell you something," "the man is a
football team," the clipped repetition, the moral certainty, the raised eyebrow at the end.

Rules, without exception:
- 1-2 sentences per manager, roughly 25-45 words. Punchy. No exclamation marks.
- VARY THE SHAPE. At most one note may open with the statistics; the rest should open with
  the verdict, the name, or a scene. Never use the same construction twice in a batch.
- "I'm telling you" in no more than one note in three; "let me tell you something" in at most
  two per batch. Silence is also Cosell.
- No metaphor may repeat across the set (e.g. "the man is a football team" once, never again).
- Use ONLY the numbers and facts in the JSON you are given. Never invent a season, a title,
  a streak, a player, a trade, or a relationship.
- Refer to each manager only by the handle given as the key. Never invent a first name.
- Every note must contain at least one real number from that manager's data.
- Cosell's bluster, not his cruelty: judge the record, never the person. No slurs, no jibes
  about appearance, family, or anything outside the numbers.
- Do not address the reader as "ladies and gentlemen" more than once across the whole set.
- Return a single JSON object mapping each handle to its note. No prose outside the JSON.

Cadence to imitate (style only, do not reuse the numbers):
{"example_handle": "Eight seasons, sixty-three wins, and not one championship — I'm telling you,
the man wins football games and loses the ones that matter, and that is the whole biography."}"""


def _parse_json_object(text: str) -> dict:
    if not text:
        return {}
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        obj = json.loads(text[i:j + 1])
        return {str(k): str(v).strip() for k, v in obj.items() if isinstance(v, (str, int, float))}
    except json.JSONDecodeError:
        return {}


def _stub_blurb(f: dict) -> str:
    """Deterministic fallback so the page always carries a note."""
    bits = [f"{f['record']} across {f['seasons']} season{'s' if f['seasons'] != 1 else ''}"]
    if f["titles"]:
        bits.append(f"{f['titles']} title{'s' if f['titles'] != 1 else ''}")
    else:
        bits.append("no title")
    if f.get("best_finish"):
        bits.append(f"best finish {f['best_finish']}")
    tail = "A career the ledger can defend." if f["pct"] >= 0.5 else "The ledger is less kind."
    return f"{', '.join(bits)}. {tail}"


def _fix_handles(blurbs: dict, facts: dict) -> dict:
    """Handles must appear exactly as they exist — models like to capitalise them."""
    import re
    out = {}
    for name, text in blurbs.items():
        if name not in facts:
            continue
        out[name] = re.sub(re.escape(name), name, str(text), flags=re.IGNORECASE)
    return out


def history_blurbs(facts: dict) -> dict:
    """One dry career note per manager, generated by DeepSeek flash (cached by input hash)."""
    import hashlib
    key = hashlib.sha256((json.dumps(facts, sort_keys=True) + BLURB_SYSTEM).encode()).hexdigest()
    if BLURB_CACHE.exists():
        try:
            cached = json.loads(BLURB_CACHE.read_text(encoding="utf-8"))
            if cached.get("key") == key and cached.get("blurbs"):
                print(f"[hof] career notes: cache hit ({len(cached['blurbs'])} managers)")
                return _fix_handles(cached["blurbs"], facts)
        except json.JSONDecodeError:
            pass

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    blurbs: dict[str, str] = {}

    def _ask(batch: dict, depth: int) -> dict:
        """One request; on truncation or a short answer, split the batch and retry."""
        if not batch:
            return {}
        body = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": BLURB_SYSTEM},
                {"role": "user", "content":
                 "Manager careers, regular season, 2019-2026. Keys are the handles to use:"
                 + chr(10) + json.dumps(batch, indent=1)
                 + chr(10) + chr(10) + "Return the JSON object now."},
            ],
            "max_tokens": 8000,
            "temperature": 0.95,
            "response_format": {"type": "json_object"},
        }
        got: dict = {}
        try:
            req = urllib.request.Request(
                DEEPSEEK_URL, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=240) as r:
                resp = json.loads(r.read())
            choice = resp["choices"][0]
            parsed = _parse_json_object(choice["message"].get("content") or "")
            got = {k: v for k, v in parsed.items() if k in batch}
            print(f"[hof] career notes: {len(batch)} asked -> {len(got)} "
                  f"(finish={choice.get('finish_reason')}, depth={depth})")
        except Exception as e:  # noqa: BLE001
            print(f"[hof] WARN career notes request failed ({e})", file=sys.stderr)

        missing = {k: v for k, v in batch.items() if k not in got}
        if missing and depth < 3:
            mid = max(1, len(missing) // 2)
            keys = list(missing)
            got.update(_ask({k: missing[k] for k in keys[:mid]}, depth + 1))
            got.update(_ask({k: missing[k] for k in keys[mid:]}, depth + 1))
        return got

    if api_key:
        items = list(facts.items())
        for i in range(0, len(items), 8):
            blurbs.update(_ask(dict(items[i:i + 8]), 0))
    else:
        print("[hof] WARN DEEPSEEK_API_KEY unset; career notes use deterministic fallback", file=sys.stderr)

    for name, f in facts.items():
        if not str(blurbs.get(name) or "").strip():
            blurbs[name] = _stub_blurb(f)

    if api_key:
        CACHE.mkdir(parents=True, exist_ok=True)
        BLURB_CACHE.write_text(json.dumps({"key": key, "blurbs": blurbs}, indent=1), encoding="utf-8")
    return _fix_handles(blurbs, facts)


# ---------------------------------------------------------------- compute

# ---------------------------------------------------------------- adjudications
# Seasons where the platform's bracket is not the whole story. Applied after the
# season loop; the manager roll-up reads "champions" when present.
CO_CHAMPIONSHIPS = {
    "2022": {
        "champions": ["ptoJoe", "kingofwakanda52"],
        "note": ("Title week abandoned: the Bills–Bengals game was suspended after "
                 "Damar Hamlin's collapse and never resumed. ptoJoe and kingofwakanda52 "
                 "agreed to split the pot and share the championship."),
    },
}


def fmt(n, nd=1):
    return f"{n:.{nd}f}" if isinstance(n, (int, float)) else "—"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _last_meeting(g: dict) -> str:
    """'2026 W9 · jasonjay86 119.7-103.0' — winner first, so the row reads plainly."""
    a_won = g["a_pts"] > g["b_pts"]
    win, lose = (g["a"], g["b"]) if a_won else (g["b"], g["a"])
    hi, lo = max(g["a_pts"], g["b_pts"]), min(g["a_pts"], g["b_pts"])
    return f"{g['season']} W{g['week']} · {win} {hi:.1f}–{lo:.1f}"


def compute(seasons_raw: list[dict], refresh: bool) -> dict:
    managers: dict[str, dict] = {}
    seasons_out = []
    games: list[dict] = []           # every completed matchup game
    weekly_scores: list[dict] = []   # every team-week score
    season_totals: list[dict] = []   # per team-season aggregates
    tx_rows: list[dict] = []
    seed_of: dict[tuple, int] = {}
    bracket_by_season: dict[str, set] = {}
    bench_rows: list[dict] = []      # one per team-week: bench points + best-ball score
    roster_points: dict[tuple, dict] = {}  # (season, roster_id, week) -> {player_id: pts}
    trades_raw: list[dict] = []      # completed trades, resolved later
    roster_week: dict[tuple, int] = {}   # (season, week, player_id) -> roster_id
    pick_players: dict[tuple, str] = {}  # (season, round, slot) -> player_id
    slot_of_user: dict[tuple, int] = {}  # (season, user_id) -> draft slot
    user_of_roster: dict[tuple, str] = {}  # (season, roster_id) -> user_id

    for s in seasons_raw:
        season = s["season"]
        lid = s["league_id"]
        for (d_season, rnd, slot), pid in (s.get("draft_slots") or {}).items():
            pick_players[(d_season, rnd, slot)] = pid
        for uid, slot in (s.get("draft_order") or {}).items():
            slot_of_user[(season, uid)] = slot
        for r in s["rosters"]:
            if r.get("owner_id"):
                user_of_roster[(season, r["roster_id"])] = str(r["owner_id"])
        reg_weeks = s["regular_weeks"]
        live_week = s.get("live_week")

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
            # Official standings come from the roster settings — they already
            # absorb stat corrections and, crucially, they do NOT include the
            # live (in-progress) week.
            w = st.get("wins") or 0
            l = st.get("losses") or 0
            t = st.get("ties") or 0
            ts[rid] = {
                "season": season, "roster_id": rid, "manager": mkey(rid),
                "team": team_names.get(rid) or "", "w": w, "l": l, "t": t,
                "pf": (st.get("fpts") or 0) + (st.get("fpts_decimal") or 0) / 100,
                "pa": (st.get("fpts_against") or 0) + (st.get("fpts_against_decimal") or 0) / 100,
                "weeks": w + l + t, "playoff_w": 0, "playoff_l": 0,
            }

        # ---- walk weeks
        for wk in sorted(s["matchups"]):
            if live_week and wk >= live_week:
                continue  # live week: scores are still moving, standings exclude it
            rows = s["matchups"][wk]
            by_match: dict[int, list] = {}
            for row in rows:
                mid = row.get("matchup_id")
                by_match.setdefault(mid, []).append(row)
                pts = row.get("points")
                rid = row.get("roster_id")
                pp = row.get("players_points") or {}
                if rid in ts:
                    roster_points[(season, rid, wk)] = pp
                for pid in (row.get("players") or []):
                    roster_week[(season, wk, pid)] = rid
                if isinstance(pts, (int, float)) and pts > 0 and rid in ts:
                    weekly_scores.append({"season": season, "week": wk, "manager": ts[rid]["manager"],
                                          "team": ts[rid]["team"], "points": float(pts),
                                          "playoff": wk >= s["playoff_start"]})
                    ts[rid]["weeks"] = ts[rid]["weeks"]  # official totals come from settings
                    starters = [x for x in (row.get("starters") or []) if x and x != "0"]
                    vals = [v for v in pp.values() if isinstance(v, (int, float))]
                    bench = round(sum(v for k, v in pp.items()
                                      if k not in starters and isinstance(v, (int, float)) and v > 0), 2)
                    best = round(sum(sorted(vals, reverse=True)[:len(starters) or 0]), 2)
                    bench_rows.append({"season": season, "week": wk, "manager": ts[rid]["manager"],
                                       "team": ts[rid]["team"], "bench": bench,
                                       "actual": round(float(pts), 2), "best": best,
                                       "playoff": wk >= s["playoff_start"]})
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
                g = {"season": season, "week": wk, "playoff": playoff,
                     "a": ts[ra]["manager"], "a_team": ts[ra]["team"], "a_pts": float(pa),
                     "b": ts[rb]["manager"], "b_team": ts[rb]["team"], "b_pts": float(pb)}
                games.append(g)
                # Sleeper's roster settings lag the most recent completed week
                # (they settle once the NFL week is finalised), so for the live
                # season the standing is rebuilt from these completed matchups.
                if live_week and not playoff:
                    for rid, own, opp in ((ra, pa, pb), (rb, pb, pa)):
                        acc = ts[rid].setdefault("_live", {"w": 0, "l": 0, "t": 0, "pf": 0.0, "pa": 0.0})
                        acc["pf"] += own
                        acc["pa"] += opp
                        if own > opp:
                            acc["w"] += 1
                        elif opp > own:
                            acc["l"] += 1
                        else:
                            acc["t"] += 1
                if playoff:
                    if pa > pb:
                        ts[ra]["playoff_w"] += 1; ts[rb]["playoff_l"] += 1
                    elif pb > pa:
                        ts[rb]["playoff_w"] += 1; ts[ra]["playoff_l"] += 1

        if live_week:
            for rid, row in ts.items():
                acc = row.pop("_live", None)
                if not acc:
                    continue
                row["w"], row["l"], row["t"] = acc["w"], acc["l"], acc["t"]
                row["pf"], row["pa"] = acc["pf"], acc["pa"]
                row["weeks"] = acc["w"] + acc["l"] + acc["t"]

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
            row["finish"] = None

        # Final finish comes from the placement games in the winners' bracket:
        # p=1 -> 1st/2nd, p=3 -> 3rd/4th, p=5 -> 5th/6th. Teams that missed the
        # bracket are ordered by regular-season seed behind the qualifiers.
        place: dict[int, int] = {}
        for g in s["winners"]:
            pos = g.get("p")
            if isinstance(pos, int) and g.get("w") is not None and g.get("l") is not None:
                place[g["w"]] = pos
                place[g["l"]] = pos + 1
        for rid, pos in place.items():
            if rid in ts:
                ts[rid]["finish"] = pos
        next_pos = (max(place.values()) + 1) if place else 1
        for row in standings:
            if row["finish"] is None:
                row["finish"] = next_pos
                next_pos += 1

        champ = ts.get(champ_rid) if champ_rid is not None else None
        runner = ts.get(runner_rid) if runner_rid is not None else None

        # ---- transactions
        for wk, rows in s["transactions"].items():
            for t in rows:
                typ = t.get("type")
                rids = t.get("roster_ids") or []
                for rid in rids:
                    if rid not in ts:
                        continue
                    tx_rows.append({"season": season, "week": wk, "type": typ,
                                    "manager": ts[rid]["manager"],
                                    "txid": t.get("transaction_id")})
                if typ == "trade" and t.get("status") == "complete" and len(rids) == 2:
                    trades_raw.append({
                        "season": season, "week": int(t.get("leg") or wk),
                        "sides": [(rid, ts[rid]["manager"]) for rid in rids],
                        "owners": {rid: roster_owner.get(rid) for rid in rids},
                        "adds": {str(k): int(v) for k, v in (t.get("adds") or {}).items()},
                        "draft_picks": t.get("draft_picks") or [],
                        "last_week": max([w for w in s["matchups"]
                                          if (not s.get("live_week") or w < s["live_week"])] or [0]),
                    })

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
            "weeks_played": max([w for w in s["matchups"]
                                 if (not s.get("live_week") or w < s["live_week"])
                                 and any((r.get("points") or 0) > 0 for r in s["matchups"][w])] or [0]),
        })

        # remember seeds for upset detection and bracket membership for playoff apps
        for rid, row in ts.items():
            seed_of[(season, row["manager"])] = row.get("seed", 99)
        for rid in bracket_rids:
            if rid in ts:
                bracket_by_season.setdefault(ts[rid]["manager"], set()).add(season)


        season_totals.extend(ts.values())

    # ---------- adjudications (co-championships etc.)
    for srow in seasons_out:
        adj = CO_CHAMPIONSHIPS.get(srow["season"])
        if not adj:
            continue
        srow["co_champions"] = True
        srow["champions"] = adj["champions"]
        recs = {r["manager"]: r for r in season_totals if r["season"] == srow["season"]}
        # a shared title is a first-place finish for both
        for r in recs.values():
            if r["manager"] in adj["champions"]:
                r["finish"] = 1
        srow["champion_record"] = " / ".join(
            f"{recs[n]['w']}-{recs[n]['l']}" for n in adj["champions"] if n in recs) or srow["champion_record"]
        srow["co_note"] = adj["note"]
        # both are champions; there is no runner-up in a shared title
        srow["runner_up"] = None
        srow["runner_up_record"] = ""
        srow["runner_up_team"] = ""

    # ---------- manager roll-up
    def m(mgr):
        return managers.setdefault(mgr, {
            "name": mgr, "team": "", "seasons": set(), "w": 0, "l": 0, "t": 0,
            "pf": 0.0, "pa": 0.0, "weeks": 0, "titles": [], "co_titles": [], "runner_ups": [],
            "playoff_apps": 0, "finishes": [], "weekly_highs": 0,
            "best_score": 0.0, "worst_score": 9999.0, "best_win": 0.0, "worst_loss": 0.0,
            "trades": 0, "waivers": 0, "fa": 0,
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
        if srow.get("co_champions"):
            for name in srow["champions"]:
                m(name)["titles"].append(srow["season"])
                m(name)["co_titles"].append(srow["season"])
        elif srow["champion"]:
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
            "titles": d["titles"], "co_titles": d.get("co_titles", []),
            "runner_ups": d["runner_ups"],
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
            "luck": round((d["w"] + 0.5 * d["t"]) - exp, 2),
            "games": g,
        })
    mrows.sort(key=lambda x: (-len(x["titles"]), -x["w"], -x["pct"]))

    # ---------- résumé grid (manager x season)
    champ_of: dict[str, dict] = {}
    runner_of: dict[str, dict] = {}
    for srow in seasons_out:
        if srow.get("co_champions"):
            for n in srow["champions"]:
                champ_of.setdefault(n, {})[srow["season"]] = "Co-champion"
        elif srow["champion"]:
            champ_of.setdefault(srow["champion"], {})[srow["season"]] = "Champion"
        if srow["runner_up"]:
            runner_of.setdefault(srow["runner_up"], {})[srow["season"]] = "Runner-up"

    live_season = seasons_out[-1]["season"] if seasons_out and seasons_out[-1].get("in_progress") else None

    resume: dict[str, list] = {}
    for row in season_totals:
        nm, seas = row["manager"], row["season"]
        if seas == live_season:
            seed = finish = None
            result = "In progress"
        else:
            seed, finish = row.get("seed"), row.get("finish")
            result = (champ_of.get(nm, {}).get(seas) or runner_of.get(nm, {}).get(seas)
                      or ("Playoff exit" if seas in bracket_by_season.get(nm, set())
                          else "Missed playoffs"))
        resume.setdefault(nm, []).append({
            "season": seas,
            "w": row["w"], "l": row["l"], "t": row["t"],
            "record": f"{row['w']}-{row['l']}" + (f"-{row['t']}" if row["t"] else ""),
            "pf": round(row["pf"], 1),
            "seed": seed,
            "finish": finish,
            "result": result,
        })
    for rows in resume.values():
        rows.sort(key=lambda r: r["season"], reverse=True)

    # ---------- bench points (lineup regret) and best-ball
    bench: dict[str, dict] = {}
    for b in bench_rows:
        if b["playoff"]:
            continue
        d = bench.setdefault(b["manager"], {"total": 0.0, "weeks": 0, "clean": 0, "worst": None})
        d["total"] += b["bench"]
        d["weeks"] += 1
        if b["bench"] <= 0.01:
            d["clean"] += 1
        if d["worst"] is None or b["bench"] > d["worst"]["bench"]:
            d["worst"] = b

    # best-ball record: replay every regular-season game using each side's
    # optimal top-N score for that week
    best_lookup = {(b["season"], b["week"], b["manager"]): b["best"] for b in bench_rows}
    # Two measures, because they answer different questions:
    #   * bestball W-L replays both sides' optimal lineups (a like-for-like test —
    #     it CAN be worse than the actual record, since opponents improve too),
    #   * wins_left counts only games where THIS team's optimal lineup would have
    #     beaten the opponent's ACTUAL score — monotone, never negative.
    bestball: dict[str, dict] = {}
    for g in games:
        if g["playoff"]:
            continue
        ba = best_lookup.get((g["season"], g["week"], g["a"]))
        bb = best_lookup.get((g["season"], g["week"], g["b"]))
        if ba is None or bb is None:
            continue
        for me, mine, theirs, my_actual, opp_actual in (
                (g["a"], ba, bb, g["a_pts"], g["b_pts"]),
                (g["b"], bb, ba, g["b_pts"], g["a_pts"])):
            d = bestball.setdefault(me, {"w": 0, "l": 0, "t": 0, "left": 0})
            if mine > theirs:
                d["w"] += 1
            elif mine < theirs:
                d["l"] += 1
            else:
                d["t"] += 1
            if my_actual < opp_actual and mine > opp_actual:
                d["left"] += 1

    for row in mrows:
        b = bench.get(row["name"], {"total": 0.0, "weeks": 0, "clean": 0, "worst": None})
        row["bench_total"] = round(b["total"], 1)
        row["bench_avg"] = round(b["total"] / b["weeks"], 1) if b["weeks"] else 0.0
        row["bench_clean"] = b["clean"]
        w = b["worst"]
        row["bench_worst"] = (f"{w['bench']:.1f} pts · {w['season']} W{w['week']}") if w else "—"
        row["bench_worst_pts"] = round(w["bench"], 1) if w else 0.0
        bb = bestball.get(row["name"], {"w": 0, "l": 0, "t": 0, "left": 0})
        row["bestball_record"] = f"{bb['w']}-{bb['l']}" + (f"-{bb['t']}" if bb["t"] else "")
        row["wins_left"] = bb["left"]
        row["bestball_gain"] = (bb["w"] - row["w"])

    # ---------- trades, resolved on what each side actually received
    names = player_names()
    trade_rows = []
    skipped_trades: list[dict] = []
    for t in trades_raw:
        sides = []
        mismatch = False
        for rid, mgr in t["sides"]:
            acquired = [pid for pid, dest in t["adds"].items() if dest == rid]
            players, total = [], 0.0
            for pid in acquired:
                pts = 0.0
                seen_on = set()
                for wk in range(t["week"] + 1, (t["last_week"] or 0) + 1):
                    pts += roster_points.get((t["season"], rid, wk), {}).get(pid, 0) or 0
                    where = roster_week.get((t["season"], wk, pid))
                    if where:
                        seen_on.add(where)
                if seen_on and rid not in seen_on:
                    mismatch = True
                nm, pos = names.get(pid, [f"Player {pid}", ""])
                players.append({"name": nm, "pos": pos, "pts": round(pts, 1)})
                total += pts
            # Picks are valued as the player eventually taken with them: same
            # window as the players (everything after the trade), counted only
            # for weeks the asset was actually on this manager's roster. A pick
            # whose draft has not happened yet is TBD, and a trade carrying a
            # TBD pick cannot be graded at all.
            picks = []
            for dp in t["draft_picks"]:
                if dp.get("owner_id") != rid:
                    continue
                pk_season = str(dp.get("season"))
                label = f"{pk_season} R{dp.get('round')}"
                # original roster -> that season's user -> draft slot -> the pick
                orig_user = user_of_roster.get((pk_season, dp.get("roster_id")))
                slot = slot_of_user.get((pk_season, orig_user)) if orig_user else None
                pid = pick_players.get((pk_season, dp.get("round"), slot)) if slot else None
                if not pid:
                    picks.append({"label": label, "player": None, "pts": 0.0, "tbd": True})
                    continue
                pts = 0.0
                for (p_season, p_wk, p_id), p_rid in roster_week.items():
                    if p_id != pid or p_rid != rid:
                        continue
                    if (p_season, p_wk) <= (t["season"], t["week"]):
                        continue
                    pts += (roster_points.get((p_season, rid, p_wk)) or {}).get(pid, 0) or 0
                nm = names.get(pid, [f"Player {pid}", ""])[0]
                picks.append({"label": label, "player": nm, "pts": round(pts, 1), "tbd": False})
                total += pts

            players.sort(key=lambda x: -x["pts"])
            picks.sort(key=lambda x: (-x["pts"], x["label"]))
            sides.append({"manager": mgr, "players": players, "total": round(total, 1),
                          "picks": picks, "picks_tbd": sum(1 for x in picks if x["tbd"])})
        if len(sides) != 2 or mismatch:
            skipped_trades.append({"season": t["season"], "week": t["week"]})
            continue
        sides.sort(key=lambda x: -x["total"])
        margin = round(sides[0]["total"] - sides[1]["total"], 1)
        weeks_elapsed = max(0, (t["last_week"] or 0) - t["week"])

        # A trade is only graded on what players actually scored AFTER it, so:
        #  - fewer than three weeks of evidence is not a verdict, it is a snapshot;
        #  - picks score nothing in this model, so a picks-for-player deal cannot be
        #    called a wash — it is simply ungraded. Both must be stated, not hidden.
        tbd = any(x["tbd"] for side in sides for x in side["picks"])
        if tbd:
            level = "Ungraded — pick not yet used"
        elif weeks_elapsed < GRADE_MIN_WEEKS:
            level = "Too recent to judge"
        else:
            level = ("Larceny" if margin >= 150 else "Clear win" if margin >= 60
                     else "Slight edge" if margin >= 15 else "A wash")

        trade_rows.append({
            "season": t["season"], "week": t["week"],
            "winner": sides[0], "loser": sides[1], "margin": margin,
            "tie": abs(margin) < 0.05,
            "weeks_elapsed": weeks_elapsed,
            "graded": not level.startswith("Too recent") and not level.startswith("Ungraded"),
            "level": level,
        })
    # graded trades first, worst heists at the top; ungraded ones sink to the bottom
    trade_rows.sort(key=lambda x: (not x["graded"], -x["margin"], x["season"]))
    if skipped_trades:
        print(f"[hof] WARN skipped {len(skipped_trades)} trade(s) with inconsistent roster "
              f"numbering: {skipped_trades}")

    trade_net: dict[str, float] = {}
    for tr in trade_rows:
        if not tr["graded"]:
            continue
        trade_net[tr["winner"]["manager"]] = trade_net.get(tr["winner"]["manager"], 0.0) + tr["margin"]
        trade_net[tr["loser"]["manager"]] = trade_net.get(tr["loser"]["manager"], 0.0) - tr["margin"]
    for row in mrows:
        row["trade_net"] = round(trade_net.get(row["name"], 0.0), 1)

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
            "closest": f"{closest['season']} W{closest['week']} · {abs(closest['a_pts']-closest['b_pts']):.2f}",
            "biggest": f"{biggest['season']} W{biggest['week']} · {abs(biggest['a_pts']-biggest['b_pts']):.1f}",
            "streak": f"{streak_holder} · {streak_len}" if streak_holder else "—",
            "last": _last_meeting(gs[-1]) if gs else "",
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

    # ---------- head-to-head matrix (regular season)
    h2h_managers = [r["name"] for r in mrows]
    cells: dict[str, dict[str, str]] = {n: {} for n in h2h_managers}
    for r in rivalries:
        a, b = r["a"], r["b"]
        if a in cells and b in cells:
            cells[a][b] = f"{r['a_wins']}-{r['b_wins']}" + (f"-{r['ties']}" if r["ties"] else "")
            cells[b][a] = f"{r['b_wins']}-{r['a_wins']}" + (f"-{r['ties']}" if r["ties"] else "")

    # ---------- per-manager H2H detail (drives the dropdown view)
    h2h_detail: dict[str, list] = {n: [] for n in h2h_managers}
    for r in rivalries:
        a, b = r["a"], r["b"]
        if a in h2h_detail:
            h2h_detail[a].append({
                "opp": b, "w": r["a_wins"], "l": r["b_wins"], "t": r["ties"],
                "games": r["games"], "avg_margin": r["avg_margin"],
                "closest": r["closest"], "biggest": r["biggest"],
                "streak": r["streak"], "last": r["last"],
            })
        if b in h2h_detail:
            h2h_detail[b].append({
                "opp": a, "w": r["b_wins"], "l": r["a_wins"], "t": r["ties"],
                "games": r["games"], "avg_margin": r["avg_margin"],
                "closest": r["closest"], "biggest": r["biggest"],
                "streak": r["streak"], "last": r["last"],
            })
    for name, rows in h2h_detail.items():
        rows.sort(key=lambda x: (-x["games"], x["opp"]))

    # ---------- career notes for the résumé grid
    blurb_facts = {}
    for m in mrows:
        blurb_facts[m["name"]] = {
            "seasons": m["seasons"], "record": m["record"], "win_pct": round(m["pct"], 3),
            "titles": len(m["titles"]), "runner_ups": len(m["runner_ups"]),
            "playoff_apps": m["playoff_apps"], "pf": round(m["pf"]), "ppg": m["ppg"],
            "best_finish": m["best_finish"], "worst_finish": m["worst_finish"],
            "longest_win_streak": m["streak_w"], "luck": m["luck"],
            "wins_left_on_bench": m["wins_left"], "trades": m["trades"],
        }
    resume_blurbs = history_blurbs(blurb_facts)

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
        "h2h_detail": h2h_detail,
        "titles_awarded": sum(len(x["titles"]) for x in mrows),
        "shared_titles": sum(1 for x in seasons_out if x.get("co_champions")),
        "resume": resume,
        "resume_blurbs": resume_blurbs,
        "trades": trade_rows,
        "totals": {
            "seasons": len(seasons_out),
            "managers": len(mrows),
            "games": len(games),
            "weeks": len(weeks_grouped),
            "first_season": seasons_out[0]["season"] if seasons_out else "",
            "last_season": seasons_out[-1]["season"] if seasons_out else "",
            "trades": len({t.get("txid") for t in tx_rows if t["type"] == "trade" and t.get("txid")}),
            "moves": len({t.get("txid") for t in tx_rows if t.get("txid")}),
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
    state = nfl_state()
    cur_season = str(state.get("season") or "")
    cur_week = int(state.get("week") or 0)
    print(f"[hof] seasons found: {[l['season'] for l in chain]} · NFL state {cur_season} W{cur_week}")
    raw = []
    for lg in chain:
        live = cur_week if str(lg.get("season")) == cur_season and cur_week else None
        s = load_season(lg, args.refresh, live_week=live)
        played = sum(1 for r in s["matchups"].values() for x in r if (x.get("points") or 0) > 0)
        print(f"[hof] {s['season']}: {len(s['rosters'])} teams · {len(s['matchups'])} weeks · {played} scored rows")
        raw.append(s)

    stats = compute(raw, args.refresh)
    Path(args.out_json).write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print(f"[hof] wrote {args.out_json}")
    if not args.data_only:
        render(stats, args.out, args.template)


if __name__ == "__main__":
    main()
