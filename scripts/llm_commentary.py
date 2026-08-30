"""
llm_commentary.py — ask MiniMax to write the newspaper-style commentary.

The prompt is deliberately tight: every name referenced in the output must
come from the data we pass in. No inventing nicknames, no fishing for personal
trivia, no real-life references. Tone is warm trash talk fit for friends &
family; commissioner credit is byline only.

Output schema (validated as JSON):
{
  "lede": {"headline": str, "deck": str, "body": str (1-2 short paragraphs)},
  "motw_blurb": str,
  "rankings_blurb": str,
  "by_the_numbers": [{"value": str, "label": str}, ...],   # 4 cards
  "closing": str
}
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path


SYSTEM_PROMPT = """You are the voice of "The Keep The Change League Tribune", a
weekly fantasy-football broadsheet in the style of a 1920s Sunday paper crossed
with vintage Sports Illustrated. The Tribune is published every Tuesday morning
for a friends-and-family league of 12 people who talk trash affectionately.

Voice rules:
- Warm, witty, fond. The audience is friends; never cruel or personal.
- Every team or owner name you mention MUST appear in the data we provide. If
  it's not in the data, don't mention it. No inventing, no inference.
- Refer to owners by their Sleeper display_name (e.g., "jasonjay86") and
  teams by their team_name when set. When team_name is empty, write "the
  {display_name} outfit".
- The commissioner is the owner of the league (visible in the data as the
  user whose user_id matches the commissioner_handle in config). Credit them
  in the byline only — never in body copy.
- No real-world facts about any person. Stay strictly in fantasy-land.
- Headlines should be punny, allusive, never mean.
- Keep total body copy under ~500 words across all sections.

You MUST respond with a single JSON object — no markdown fences, no preamble."""


def build_user_prompt(rankings: dict, site_cfg: dict) -> str:
    season_type = rankings.get("season_type", "regular")
    wk = rankings.get("week")
    league_name = rankings.get("league", "the league")
    motw = rankings.get("matchup_of_week")

    # Compact rankings table for the LLM
    rows = []
    for r in rankings["rankings"]:
        owner = (r.get("owner") or {}).get("display_name", "?")
        team  = (r.get("owner") or {}).get("team_name", "") or "(no team name)"
        rows.append({
            "rank":        r["rank"],
            "owner":       owner,
            "team":        team,
            "record":      f"{r['wins']}-{r['losses']}" + (f"-{r['ties']}" if r.get('ties') else ""),
            "points_for":  round(r["points_for"], 1),
            "power_score": round(r["power_score"], 1),
            "sos":         round(r.get("sos_factor", 1.0), 2),
        })

    motw_payload = None
    if motw:
        motw_payload = {
            "status": motw["status"],
            "team_a": motw["team_a"],
            "team_b": motw["team_b"],
        }

    payload = {
        "league":       league_name,
        "season":       rankings.get("season"),
        "week":         wk,
        "season_type":  season_type,
        "is_opening":   all(r["wins"] == 0 and r["losses"] == 0 for r in rankings["rankings"]),
        "rankings":     rows,
        "matchup_of_week": motw_payload,
        "commissioner_handle": site_cfg.get("commissioner_handle"),
    }

    return json.dumps(payload, indent=2)


def extract_json(text: str) -> dict:
    """Tolerate models that wrap JSON in ```json ... ``` despite the instruction."""
    text = (text or "").strip()
    if not text:
        raise SystemExit("[llm_commentary] API returned empty content. See logs above for raw response.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Try to grab the first {...} block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            print(f"[llm_commentary] could not parse JSON. First 600 chars of raw response:", file=sys.stderr)
            print(text[:600], file=sys.stderr)
            raise SystemExit(f"[llm_commentary] JSON parse error: {e}")
        return json.loads(m.group(0))


def call_minimax(messages: list, model: str, base_url: str, api_key: str, max_tokens: int = 1800) -> str:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.85,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    return resp["choices"][0]["message"]["content"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rankings",   default="rankings.json")
    ap.add_argument("--config",     default="config.json")
    ap.add_argument("--out",        default="commentary.json")
    ap.add_argument("--dry-run",    action="store_true",
                    help="Skip the API call and write a stub commentary file.")
    args = ap.parse_args()

    rankings = json.loads(Path(args.rankings).read_text())
    cfg      = json.loads(Path(args.config).read_text())
    llm_cfg  = cfg.get("llm", {})

    user_prompt = build_user_prompt(rankings, cfg)

    if args.dry_run or not os.environ.get("MINIMAX_API_KEY"):
        stub = {
            "lede": {
                "headline": "Twelve Teams Lace Up; The Tribune Watches",
                "deck":     "Opening week brings no answers, only possibilities.",
                "body":     "The season opens with twelve contenders and no prior evidence. Every roster is a closed envelope; every prediction is a guess dressed in a blazer. The Tribune will return next week with receipts.",
            },
            "motw_blurb":     "A meeting of undefeated outfits. One of them leaves the week still believing.",
            "rankings_blurb": "All tied at the gate. The Tribune declines to invent a favorite — yet.",
            "by_the_numbers": [
                {"value": "12", "label": "Hopefuls"},
                {"value": "0",  "label": "Games Settled"},
                {"value": "—",  "label": "Power Spread"},
                {"value": "1",  "label": "Week Ahead"},
            ],
            "closing": "Tip-off approaches. Read the standings again Sunday night.",
        }
        Path(args.out).write_text(json.dumps(stub, indent=2))
        print(f"[llm_commentary] wrote {args.out} (stub, no API key)")
        return

    raw = call_minimax(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        model=llm_cfg.get("model", "MiniMax-M3"),
        base_url=llm_cfg.get("base_url", "https://api.minimax.io/v1").rstrip("/"),
        api_key=os.environ["MINIMAX_API_KEY"],
        max_tokens=llm_cfg.get("max_tokens", 1800),
    )
    commentary = extract_json(raw)
    Path(args.out).write_text(json.dumps(commentary, indent=2))
    print(f"[llm_commentary] wrote {args.out} (from API)")


if __name__ == "__main__":
    main()