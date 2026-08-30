"""
llm_commentary.py — ask MiniMax to write the Tribune commentary.

Voice: John Madden calling the game from your couch.
Length: ~330 words across lede.body + motw_blurb + rankings_blurb + closing.
Tone: warm trash talk for friends & family, with commissioner-supplied
      relationship color (brothers, father, ex-wife's uncle, geography,
      the firm) drawn from league_context.json.

Guardrails:
- Every name/relationship/location referenced must be in the data we pass
  in OR in league_context.json. No inventing facts, no inference.
- The commissioner credit is byline-only; no body copy references the
  commissioner role.
- league_context.json is private; its facts are only used to enrich voice,
  not to publish beyond what's already on the public site.
- Strict JSON output. motw_blurb / rankings_blurb / closing are STRINGS,
  not nested objects.

Output schema (validated after extraction):
{
  "lede": {"headline": str, "deck": str, "body": str (1 short paragraph)},
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
import urllib.error
import urllib.request
from pathlib import Path


SYSTEM_PROMPT = """You are the voice of "The Keep The Change League Tribune", a
weekly fantasy-football broadsheet. You are NOT a 1920s newspaper. You are
NOT a sportswriter with a byline. You are John Madden calling this fantasy
game from the couch next to the commissioner.

VOICE — John Madden:
- Plainspoken. Conversational. Short sentences. Choppy rhythm.
- Full of football mechanics: "boom", "bang-bang", "here's a guy who...",
  "now watch this", "you can see it on the tape", "let me tell ya".
- Loves tangents about HOW plays work, not what they mean.
- Affectionate confusion: "I don't know what's going on here, but...".
- Half-thoughts and self-corrections mid-sentence.
- Diagrams with words. "If this guy goes here, that guy goes there..."
- No purple prose. No headlines-puns in body copy. No "lo! the ledger..."
- Speak it out loud. Imagine the commissioner is half-watching.

LENGTH — TOTAL ~280-320 WORDS, HARD CEILING 350:
- lede.body:        ~90 words (one paragraph, choppy)
- motw_blurb:       ~70 words
- rankings_blurb:   ~90 words
- closing:          ~25 words
That's the total. Stay tight. If you go over 350 words the page gets
long and you start sounding like a writer, not Madden. Cut anything that
doesn't sound like talking. When in doubt, leave it out.

FACTS — only what's in the data and league_context.json:
- Every team, owner, score, rank must come from the rankings/matchup payload.
- Relationships, geography, family dynamics, and work history come from
  league_context.json. Use them as COLOR, not as facts to editorialize on.
- Do NOT invent facts that aren't listed (no specific employer roles, no
  specific cities not listed, no specific family stories).
- Refer to owners by their Sleeper display_name or their generic team name.
- When brothers play each other or play the father, you may note it in the
  warm trash-talk way. Never cruel. Never make fun of family itself.
- The new guy (Trey) is the odd one out and may be gently ribbed.

OUTPUT — strict JSON, exact shape:
- One JSON object. No markdown fences. No preamble.
- Keys (exact): "lede", "motw_blurb", "rankings_blurb", "by_the_numbers", "closing"
- lede:           OBJECT with "headline" (string), "deck" (string), "body" (string, ~110 words)
- motw_blurb:     STRING, plain prose, ~80 words
- rankings_blurb: STRING, plain prose, ~110 words
- by_the_numbers: ARRAY of EXACTLY 4 OBJECTS, each with "value" (string) and "label" (string)
- closing:        STRING, plain prose, ~30 words

CRITICAL: motw_blurb, rankings_blurb, closing MUST be plain strings.
Only "lede" uses the nested object form."""


def load_league_context(repo_root: Path) -> dict:
    """Load commissioner-only narrative context. Empty dict if missing."""
    ctx_path = repo_root / "league_context.json"
    if not ctx_path.exists():
        return {}
    try:
        return json.loads(ctx_path.read_text())
    except Exception as e:
        print(f"[llm_commentary] could not load league_context.json: {e}", file=sys.stderr)
        return {}


def enrich_members(rows: list[dict], context: dict) -> list[dict]:
    """
    Add commissioner-supplied color to each roster row so the LLM can use
    it without having to look up the context separately.
    """
    members = (context or {}).get("members") or {}
    for row in rows:
        info = members.get(row["owner"]) or {}
        row["role"]         = info.get("role") or ""
        row["location"]     = info.get("location") or ""
        row["notes"]        = info.get("notes") or ""
        row["team_label"]   = row["team"] if row["team"] and row["team"] != "(no team name)" else (
                              info.get("generic_team_name") or row["owner"]
                             )
    return rows


def build_user_prompt(rankings: dict, site_cfg: dict, context: dict) -> str:
    season_type = rankings.get("season_type", "regular")
    wk = rankings.get("week")
    league_name = rankings.get("league", "the league")
    motw = rankings.get("matchup_of_week")

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

    rows = enrich_members(rows, context)

    motw_payload = None
    if motw:
        motw_payload = {
            "status": motw["status"],
            "team_a": motw["team_a"],
            "team_b": motw["team_b"],
        }

    payload = {
        "league":              league_name,
        "season":              rankings.get("season"),
        "week":                wk,
        "season_type":         season_type,
        "is_opening":          all(r["wins"] == 0 and r["losses"] == 0 for r in rankings["rankings"]),
        "rankings":            rows,
        "matchup_of_week":     motw_payload,
        "commissioner_handle": site_cfg.get("commissioner_handle"),
        "family_dynamics":     (context or {}).get("family_dynamics") or {},
        "geography":           (context or {}).get("geography") or {},
        "professional_culture":(context or {}).get("professional_culture") or {},
        "voice_directives":    (context or {}).get("voice_directives") or {},
    }

    return json.dumps(payload, indent=2)


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise SystemExit("[llm_commentary] API returned empty content. See logs above for raw response.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            print(f"[llm_commentary] could not parse JSON. First 600 chars of raw response:", file=sys.stderr)
            print(text[:600], file=sys.stderr)
            raise SystemExit(f"[llm_commentary] JSON parse error: {e}")
        return json.loads(m.group(0))


def call_minimax(system: str, user: str, model: str, base_url: str, api_key: str, max_tokens: int = 1800) -> str:
    """
    MiniMax Anthropic-compatible Messages API.

    Endpoint: {base_url}/messages  (NOT /chat/completions)
    Auth: Authorization: Bearer <key>
    Body: messages use content-blocks [{type:"text", text:"..."}],
          system is a top-level string field, NOT a message.
    Response: content is an array of blocks; text comes from the block
              where type == "text".
    """
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.85,
        "system": system,
        "messages": [{"role": "user", "content": [{"type": "text", "text": user}]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"[llm_commentary] HTTP {e.code} from {req.full_url}: {err_body[:500]}", file=sys.stderr)
        raise

    blocks = resp.get("content") or []
    text_chunks = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    content = "".join(text_chunks).strip()
    if not content:
        print(f"[llm_commentary] empty content. Full response: {json.dumps(resp)[:500]}", file=sys.stderr)
        raise SystemExit("[llm_commentary] empty content")
    return content


STUB_FALLBACK = {
    "lede": {
        "headline": "BOOM — Week One",
        "deck":     "Twelve teams, zero games, all tied up. The Tribune is on the couch.",
        "body":     "Alright, alright, here we are. Twelve managers, twelve rosters, twelve zeroes across the board. You can't tell me anything yet — nobody's played. It's like trying to call a baseball game when the pitcher's still in the dugout. We got the brothers squaring off. We got the old man in there. We got the new guy, Trey, who I haven't seen enough tape on. That's gonna be the year. Let's watch some football.",
    },
    "motw_blurb":     "BOOM — Jason versus the east-coast brother. This is the one. Commissioner's club against the eldest. Blood on the field, kinda. Dad's watching. The new guy has the byline too if he wants it.",
    "rankings_blurb": "Now watch this — here's a guy who's ranked number one. And here's another guy ranked number one. They're all ranked number one, that's the problem. Tiebreakers? None. Schedule? Same. Power score? Fifty flat, the whole board. You can't rank 'em yet. You just gotta play 'em.",
    "by_the_numbers": [
        {"value": "12", "label": "Coaches Drawing Up Plays"},
        {"value": "0",  "label": "Games In The Books"},
        {"value": "1",  "label": "Brother-on-Brother Battle Looming"},
        {"value": "?",  "label": "Predictions From The Tribune"},
    ],
    "closing": "That's the week. Same couch, same friends, next Tuesday. BOOM.",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rankings",        default="rankings.json")
    ap.add_argument("--config",          default="config.json")
    ap.add_argument("--league-context",  default="league_context.json")
    ap.add_argument("--out",             default="commentary.json")
    ap.add_argument("--dry-run",         action="store_true",
                    help="Skip the API call and write a stub commentary file.")
    args = ap.parse_args()

    rankings = json.loads(Path(args.rankings).read_text())
    cfg      = json.loads(Path(args.config).read_text())
    llm_cfg  = cfg.get("llm", {})
    context  = load_league_context(Path(".").resolve())

    user_prompt = build_user_prompt(rankings, cfg, context)

    if args.dry_run or not os.environ.get("MINIMAX_API_KEY"):
        Path(args.out).write_text(json.dumps(STUB_FALLBACK, indent=2))
        print(f"[llm_commentary] wrote {args.out} (stub, no API key)")
        return

    raw = call_minimax(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            model=llm_cfg.get("model", "MiniMax-M3"),
            base_url=llm_cfg.get("base_url", "https://api.minimax.io/anthropic/v1").rstrip("/"),
            api_key=os.environ["MINIMAX_API_KEY"],
            max_tokens=llm_cfg.get("max_tokens", 1200),
        )
    commentary = extract_json(raw)

    required = {"lede", "motw_blurb", "rankings_blurb", "by_the_numbers", "closing"}
    missing = required - set(commentary.keys())
    if missing:
        print(f"[llm_commentary] LLM JSON missing fields {missing}. Falling back to stub.", file=sys.stderr)
        print(f"[llm_commentary] Got keys: {list(commentary.keys())}", file=sys.stderr)
        commentary = STUB_FALLBACK

    Path(args.out).write_text(json.dumps(commentary, indent=2))
    print(f"[llm_commentary] wrote {args.out} (from API)")


if __name__ == "__main__":
    main()