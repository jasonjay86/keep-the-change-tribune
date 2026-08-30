# The Keep The Change League Tribune

A weekly fantasy-footland broadsheet for the Keep The Change Sleeper league.
Renders a vintage-newspaper Power Ranking every Tuesday morning and publishes
it to GitHub Pages.

**Live site:** `https://jjfro.github.io/keep-the-change-tribune/`

## How it works

```
Sleeper API  ──►  fetch_sleeper.py  ──►  power_rankings.py  ──►  llm_commentary.py  ──►  render.py  ──►  index.html  ──►  GitHub Pages
                                                                  (MiniMax)
```

1. **fetch_sleeper.py** — pulls the league, rosters, users, and current-week matchups from the public Sleeper API (`api.sleeper.app`).
2. **power_rankings.py** — computes a Composite Power Score per team (40% win % + 25% PF/game + 20% strength-of-schedule + 15% all-play record), normalized so #1 = 100 and last = 40. Picks the Matchup of the Week.
3. **llm_commentary.py** — sends a JSON bundle of the rankings to MiniMax (OpenAI-compatible endpoint) with a strict system prompt. Outputs a JSON object with lede, matchup blurb, rankings blurb, by-the-numbers cards, and a closing line.
4. **render.py** — feeds everything into a Jinja2 template (`templates/tribune.html.j2`) styled by `static/style.css`. Writes `index.html` (latest) and `editions/week-NN.html` (archive copy).

The whole thing is wired together by `.github/workflows/weekly.yml`, which runs every Tuesday at 9 AM ET and on manual trigger.

## Local development

```bash
python -m pip install -r requirements.txt

# Pull live data and compute rankings
python scripts/fetch_sleeper.py
python scripts/power_rankings.py

# Render with stub commentary (no API key required)
python scripts/llm_commentary.py --dry-run
python scripts/render.py

# Render with real MiniMax commentary
export MINIMAX_API_KEY=sk-...
python scripts/llm_commentary.py
python scripts/render.py
```

Open `index.html` in a browser. The page uses Google Fonts — first load may take a second.

## Configuration

- `config.json` — league ID, site title, palette, ranking weights, LLM endpoint.
- `league_context.json` — **commissioner-only** notes the LLM uses for
  relationship color and warm-trash-talk: family dynamics, geography, work
  culture, voice directives (e.g. "John Madden, ~330 words"). Edit this
  file to teach the Tribune about your league without rewriting prompts.
- Generic team names are pulled from `league_context.json` when an owner
  has not set a Sleeper `team_name`.

## Deployment setup

Two things you do *once*:

1. **GitHub Pages.** Repo Settings → Pages → Source: "Deploy from a branch" → Branch: `main` → `/ (root)`. The site goes live at `https://jjfro.github.io/keep-the-change-tribune/` after the first push.
2. **MiniMax API key.** Repo Settings → Secrets and variables → Actions → New repository secret. Name: `MINIMAX_API_KEY`. Value: your MiniMax API key.

That's it. The scheduled run on the next Tuesday will publish the first edition.

## Manual run

Actions tab → "Weekly Tribune" → "Run workflow". Optional checkbox to dry-run with stub commentary (no API cost).

## Costs

- GitHub Pages: $0 (free for public repos)
- GitHub Actions: $0 (well under 2,000 min/month free tier — this workflow runs ~30 seconds)
- MiniMax API: ~$0.001–0.01 per weekly run depending on commentary length

Total monthly cost at one run per week: **pennies to a dime**.

## Configuration

`config.json` controls the league ID, site title, color palette, ranking weights, and LLM endpoint. Edit and re-run the workflow — no rebuild required.

## Edge cases handled

- **Pre-season / opening week:** zeroed rankings table, "Opening Day Spectator" tone, schedule preview as Matchup of the Week.
- **Off-season:** the system gracefully renders even with no recent games.
- **Playoffs:** the LLM prompt branches on `season_type` from the NFL state endpoint.
- **Tied rankings:** tiebreakers are total PF → head-to-head → last week's PF.
- **Missing team names:** the template and LLM prompt both fall back to "the {display_name} outfit".
- **Missing API key:** the workflow uses stub commentary and still publishes a valid page.
- **No data changes between weeks:** the workflow detects and skips the commit.

## License

MIT.