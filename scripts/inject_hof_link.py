"""
inject_hof_link.py — ensure every published Tribune page carries the
Hall of Fame link.

The render pipeline (render.py + templates/tribune.html.j2) already emits the
nav for every NEW edition. This script is the one-off/backfill tool for pages
that were rendered before the HOF existed: it inserts the same nav before
</header> and adds the footer link. Idempotent — safe to run any number of
times, and safe to leave in the cron.

Usage: python scripts/inject_hof_link.py [--root .]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

MARKER = 'class="tribune-nav"'

NAV = """  <nav class="tribune-nav">
    <a href="{p}hof.html">The Hall of Fame</a>
    <span class="nav-dot">·</span>
    <a href="{p}editions/index.html">Past Editions</a>
  </nav>
  <div class="rule rule-single"></div>
"""


def patch_file(path: Path, prefix: str) -> bool:
    html = path.read_text(encoding="utf-8")
    original = html
    nl = "\r\n" if "\r\n" in html else "\n"

    if MARKER not in html and "</header>" in html:
        block = NAV.format(p=prefix).replace("\n", nl)
        html = html.replace("</header>", block + "</header>", 1)

    # footer: make sure a Hall of Fame link exists in the colophon
    if 'class="archive-note"' in html and f'{prefix}hof.html">The Hall of Fame</a>' in html:
        # already there (either via nav insertion above or a prior run) — but the
        # nav alone is not the footer; only skip if the footer itself carries it
        pass
    m = re.search(r'<p class="archive-note">(.*?)</p>', html, re.S)
    if m and 'hof.html' not in m.group(1):
        inner = m.group(1)
        new_inner = inner.replace(
            f'<a href="{prefix}editions/index.html">Past Editions</a>',
            f'<a href="{prefix}hof.html">The Hall of Fame</a> \u00b7' + nl + "    "
            f'<a href="{prefix}editions/index.html">Past Editions</a>', 1)
        html = html[:m.start(1)] + new_inner + html[m.end(1):]

    if html != original:
        path.write_text(html, encoding="utf-8")
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    root = Path(args.root)

    targets = []
    for f in sorted(root.glob("*.html")):
        if f.name in {"hof.html"}:
            continue
        targets.append((f, ""))
    for f in sorted((root / "editions").glob("*.html")):
        targets.append((f, "../"))

    changed = 0
    for f, prefix in targets:
        try:
            if patch_file(f, prefix):
                changed += 1
                print(f"[inject] patched {f}")
        except Exception as e:  # noqa: BLE001
            print(f"[inject] WARN {f}: {e}")
    print(f"[inject] {changed} file(s) updated of {len(targets)} scanned")


if __name__ == "__main__":
    main()
