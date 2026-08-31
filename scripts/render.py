"""
render.py — assemble the full page context and write index.html.

Usage: python render.py [--rankings rankings.json] [--commentary commentary.json] [--out index.html] [--archive editions/week-NN.html]
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


def owner_label(team_name: str | None, display_name: str | None) -> str:
    return (team_name or "").strip() or (display_name or "Unknown")


def build_context(rankings: dict, commentary: dict | None, site_cfg: dict,
                   path_prefix: str = "") -> dict:
    wk = rankings.get("week")
    season = rankings.get("season")
    season_type = rankings.get("season_type", "regular")

    # Sensible defaults if commentary missing (template shouldn't blow up)
    lede          = (commentary or {}).get("lede")
    motw_blurb    = (commentary or {}).get("motw_blurb")
    pick          = (commentary or {}).get("pick")
    rankings_blurb= (commentary or {}).get("rankings_blurb")
    by_the_numbers = (commentary or {}).get("by_the_numbers") or [
        {"value": rankings["rankings"][0]["power_score"],     "label": "Top Power Score"},
        {"value": rankings["rankings"][-1]["power_score"],    "label": "Cellar Power Score"},
        {"value": len(rankings["rankings"]),                  "label": "Teams in the Hunt"},
        {"value": wk or "?",                                   "label": "Week Number"},
    ]
    closing = (commentary or {}).get("closing")

    return {
        "site_title":       site_cfg["site_title"],
        "tagline":          site_cfg.get("tagline", ""),
        "season":           season,
        "week":             wk,
        "season_type":      season_type,
        "rankings":         rankings["rankings"],
        "matchup_of_week":  rankings.get("matchup_of_week"),
        "lede":             lede,
        "motw_blurb":       motw_blurb,
        "pick":             pick,
        "rankings_blurb":   rankings_blurb,
        "by_the_numbers":   by_the_numbers,
        "closing":          closing,
        "path_prefix":      path_prefix,  # "" for index.html, "../" for editions/
        "generated_at":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rankings",   default="rankings.json")
    ap.add_argument("--commentary", default="commentary.json")
    ap.add_argument("--config",     default="config.json")
    ap.add_argument("--template",   default="templates/tribune.html.j2")
    ap.add_argument("--out",        default="index.html")
    ap.add_argument("--archive",    default=None,
                    help="If set, also write a copy to this path (e.g. editions/week-1.html)")
    args = ap.parse_args()

    env = Environment(
        loader=FileSystemLoader(Path(args.template).parent),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["capitalize"] = lambda s: (s or "").capitalize()
    template = env.get_template(Path(args.template).name)

    rankings   = json.loads(Path(args.rankings).read_text())
    site_cfg   = json.loads(Path(args.config).read_text())
    commentary_path = Path(args.commentary)
    commentary = json.loads(commentary_path.read_text()) if commentary_path.exists() else None

    context = build_context(rankings, commentary, site_cfg)
    html = template.render(**context)

    Path(args.out).write_text(html)
    print(f"[render] wrote {args.out} ({len(html)} bytes)")

    if args.archive:
        Path(args.archive).parent.mkdir(parents=True, exist_ok=True)
        # Re-render with path_prefix="../" so the archive's stylesheet
        # link and any other relative asset paths resolve correctly.
        archive_context = build_context(rankings, commentary, site_cfg, path_prefix="../")
        archive_html = template.render(**archive_context)
        Path(args.archive).write_text(archive_html)
        print(f"[render] archived {args.archive}")


if __name__ == "__main__":
    main()