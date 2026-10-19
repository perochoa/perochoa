#!/usr/bin/env python3
"""Build the GitHub profile: README.md from profile.toml, and animated SVG cards from live GitHub data.

    python scripts/generate.py               # fetch data, write cards to dist/
    python scripts/generate.py --cached      # reuse dist/data.json instead of calling the API
    python scripts/generate.py --readme      # render README.md (no network)
    python scripts/generate.py --check       # exit 1 if README.md is out of date with the config
    python scripts/generate.py --preview     # also write dist/preview.html, wired to the local cards

Token: $PROFILE_TOKEN, then $GITHUB_TOKEN, then `gh auth token`. Stdlib only (Python 3.11+).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "profile.toml"
TEMPLATE = ROOT / "templates" / "README.md"
README = ROOT / "README.md"
DIST = ROOT / "dist"
THEMES = ("dark", "light")

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"
HEAVY = "'Arial Black','Helvetica Neue',Helvetica,Arial,sans-serif"
MONO_ADVANCE = 0.6  # monospace glyph width per em


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


# --------------------------------------------------------------------------- data


def token() -> str:
    for var in ("PROFILE_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("no token: set PROFILE_TOKEN or GITHUB_TOKEN, or log in with `gh auth login`")


def graphql(query: str, variables: dict, auth: str, allow_missing: bool = False) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {auth}", "User-Agent": "perochoa-profile"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    errors = body.get("errors") or []
    if allow_missing:  # a featured repo that's private to this token or deleted comes back NOT_FOUND
        for err in (e for e in errors if e.get("type") == "NOT_FOUND"):
            print(f"warning: {err['message']}", file=sys.stderr)
        errors = [e for e in errors if e.get("type") != "NOT_FOUND"]
    if errors:
        sys.exit(f"graphql error: {errors}")
    return body["data"]


REPO_FIELDS = """
  name nameWithOwner description url stargazerCount forkCount pushedAt isFork isArchived isPrivate
  primaryLanguage { name color }
"""

USER_QUERY = f"""
query($login: String!) {{
  user(login: $login) {{
    name createdAt
    followers {{ totalCount }}
    repositories(ownerAffiliations: OWNER, privacy: PUBLIC, first: 100, orderBy: {{field: PUSHED_AT, direction: DESC}}) {{
      totalCount
      nodes {{
        {REPO_FIELDS}
        languages(first: 20, orderBy: {{field: SIZE, direction: DESC}}) {{ edges {{ size node {{ name color }} }} }}
      }}
    }}
  }}
}}
"""


def fetch(cfg: dict) -> dict:
    auth = token()
    login = cfg["user"]["login"]
    user = graphql(USER_QUERY, {"login": login}, auth)["user"]

    # contributionsCollection spans at most a year, so ask for each year since the account was made.
    now = dt.datetime.now(dt.UTC)
    first = int(user["createdAt"][:4])
    years = []
    for year in range(first, now.year + 1):
        start = f"{year}-01-01T00:00:00Z"
        end = now.isoformat() if year == now.year else f"{year}-12-31T23:59:59Z"
        years.append(
            f'y{year}: contributionsCollection(from: "{start}", to: "{end}") {{'
            " totalCommitContributions totalPullRequestContributions totalIssueContributions"
            " totalPullRequestReviewContributions restrictedContributionsCount"
            " contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } } }"
        )
    contrib = graphql(f"query($login: String!) {{ user(login: $login) {{ {' '.join(years)} }} }}", {"login": login}, auth)

    # The last 53 weeks for the heatmap, as GitHub draws them on the profile page.
    recent = graphql(
        "query($login: String!) { user(login: $login) { contributionsCollection {"
        " totalCommitContributions totalPullRequestContributions totalIssueContributions"
        " contributionCalendar { totalContributions weeks { contributionDays { date contributionCount weekday } } } } } }",
        {"login": login},
        auth,
    )["user"]["contributionsCollection"]

    featured = {}
    aliases = []
    for i, item in enumerate(cfg.get("featured", [])):
        owner, _, name = item["repo"].rpartition("/")
        aliases.append(f'r{i}: repository(owner: "{owner or login}", name: "{name}") {{ {REPO_FIELDS} }}')
    if aliases:
        repos = graphql(f"query {{ {' '.join(aliases)} }}", {}, auth, allow_missing=True)
        for i, item in enumerate(cfg["featured"]):
            if repos.get(f"r{i}"):
                featured[item["repo"]] = repos[f"r{i}"]

    return {
        "fetched_at": now.isoformat(),
        "user": user,
        "years": contrib["user"],
        "recent": recent,
        "featured": featured,
    }


def streaks(days: dict[str, int], today: dt.date) -> tuple[int, int]:
    """Current and longest run of days with contributions. Today not counting yet doesn't break the streak."""
    longest = run = 0
    prev = None
    for date in sorted(d for d, n in days.items() if n > 0):
        day = dt.date.fromisoformat(date)
        run = run + 1 if prev and (day - prev).days == 1 else 1
        longest = max(longest, run)
        prev = day
    current = 0
    day = today if days.get(today.isoformat(), 0) > 0 else today - dt.timedelta(days=1)
    while days.get(day.isoformat(), 0) > 0:
        current += 1
        day -= dt.timedelta(days=1)
    return current, longest


def summarize(cfg: dict, data: dict) -> dict:
    user = data["user"]
    repos = [r for r in user["repositories"]["nodes"] if not r["isFork"]]
    days: dict[str, int] = {}
    total = 0
    for year in data["years"].values():
        total += year["contributionCalendar"]["totalContributions"] + year["restrictedContributionsCount"]
        for week in year["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                days[day["date"]] = day["contributionCount"]
    today = dt.datetime.fromisoformat(data["fetched_at"]).date()
    current, longest = streaks(days, today)
    recent = data["recent"]

    lang_cfg = cfg.get("cards", {}).get("languages", {})
    excluded = set(lang_cfg.get("exclude", []))
    sizes: dict[str, int] = {}
    counts: dict[str, int] = {}
    colors: dict[str, str] = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in excluded:
                continue
            sizes[name] = sizes.get(name, 0) + edge["size"]
            counts[name] = counts.get(name, 0) + 1
            colors[name] = edge["node"]["color"] or "#8d96a0"
    sw, cw = lang_cfg.get("size_weight", 1.0), lang_cfg.get("count_weight", 0.0)
    scores = {n: (sizes[n] ** sw) * (counts[n] ** cw) for n in sizes}
    top = sorted(scores, key=scores.get, reverse=True)[: lang_cfg.get("top", 8)]
    score_sum = sum(scores[n] for n in top) or 1
    languages = [{"name": n, "color": colors[n], "pct": 100 * scores[n] / score_sum} for n in top]

    return {
        "today": today,
        "created": dt.datetime.fromisoformat(user["createdAt"]).date(),
        "followers": user["followers"]["totalCount"],
        "repos": len(repos),
        "stars": sum(r["stargazerCount"] for r in repos),
        "total": total,
        "year_commits": recent["totalCommitContributions"],
        "year_prs": recent["totalPullRequestContributions"],
        "year_issues": recent["totalIssueContributions"],
        "year_total": recent["contributionCalendar"]["totalContributions"],
        "active_days": sum(1 for wk in recent["contributionCalendar"]["weeks"] for d in wk["contributionDays"] if d["contributionCount"]),
        "weeks": recent["contributionCalendar"]["weeks"],
        "current_streak": current,
        "longest_streak": longest,
        "languages": languages,
        "featured": data["featured"],
    }


# --------------------------------------------------------------------------- svg helpers


def palette(cfg: dict, theme: str) -> dict:
    return {**cfg["theme"], **cfg["theme"][theme]}


def svg(width: int, height: int, body: str, style: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"'
        f' role="img" aria-labelledby="t"><title id="t">{esc(title)}</title>'
        f"<style>{style}"
        "@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}"
        ".in{animation:fade .6s ease-out both}"
        # resting styles are the final frame, so no-animation renderers and reduced motion show finished cards
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
        f"</style>{body}</svg>\n"
    )


def card_frame(p: dict, width: int, height: int, title: str, icon: str) -> str:
    return (
        f'<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="12" fill="{p["card"]}" stroke="{p["border"]}"/>'
        f'<g class="in"><text x="24" y="38" font-family="{SANS}" font-size="15" font-weight="700" fill="{p["fg"]}">'
        f'<tspan fill="{p["accent_2"]}">{icon}</tspan>  {esc(title)}</text></g>'
    )


def mix(hex_a: str, hex_b: str, t: float) -> str:
    a = [int(hex_a[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def compact(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def ago(iso: str, now: dt.date) -> str:
    days = (now - dt.datetime.fromisoformat(iso).date()).days
    for unit, size in (("y", 365), ("mo", 30), ("w", 7), ("d", 1)):
        if days >= size:
            return f"{days // size}{unit} ago"
    return "today"


def wrap(text: str, width: int, lines: int) -> list[str]:
    words, out, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
            if len(out) == lines:
                break
        else:
            line = f"{line} {word}".strip()
    else:
        out.append(line)
        return out
    out[-1] = out[-1][: width - 1].rstrip() + "…"
    return out


# --------------------------------------------------------------------------- cards


def hero(cfg: dict, s: dict) -> str:
    p = palette(cfg, "dark")
    w, h = 1200, 400
    hero_cfg, user = cfg["hero"], cfg["user"]
    taglines = hero_cfg["taglines"]
    font, x0, y0 = 24, 72, 330
    char_w = font * MONO_ADVANCE
    prompt = f"{user['handle']} ~ %"
    tx = x0 + (len(prompt) + 1) * char_w

    # Typewriter: each tagline gets a slot of the loop. A clip rect slides right in character-sized steps to type
    # it, holds, then slides back to delete it. The cursor rides the same keyframes.
    slot = 100 / len(taglines)
    css, clips, texts, cursor = [], [], [], []
    for i, line in enumerate(taglines):
        n, lw = len(line), len(line) * char_w
        a, b, c, d = (i * slot + f * slot for f in (0, 0.42, 0.78, 0.94))
        steps = f"animation-timing-function:steps({n})"
        css.append(
            f"@keyframes t{i}{{0%,{a:.2f}%{{transform:translateX(0);{steps}}}"
            f"{b:.2f}%,{c:.2f}%{{transform:translateX({lw:.1f}px);{steps}}}{d:.2f}%,100%{{transform:translateX(0)}}}}"
            f".t{i}{{transform:translateX({lw if i == 0 else 0:.1f}px);animation:t{i} {len(taglines) * 4.2:.1f}s infinite}}"
        )
        cursor += [
            f"{a:.2f}%{{transform:translateX(0);{steps}}}",
            f"{b:.2f}%,{c:.2f}%{{transform:translateX({lw:.1f}px);{steps}}}",
            f"{d:.2f}%{{transform:translateX(0)}}",
        ]
        clips.append(f'<clipPath id="c{i}"><rect class="t{i}" x="{tx - lw:.1f}" y="{y0 - 30}" width="{lw:.1f}" height="40"/></clipPath>')
        texts.append(
            f'<text clip-path="url(#c{i})" x="{tx:.1f}" y="{y0}" textLength="{lw:.1f}" lengthAdjust="spacing"'
            f' font-family="{MONO}" font-size="{font}" fill="#fff">{esc(line)}</text>'
        )
    css.append(f"@keyframes cur{{{''.join(cursor)}100%{{transform:translateX(0)}}}}")
    css.append(f".cur{{transform:translateX({len(taglines[0]) * char_w:.1f}px);animation:cur {len(taglines) * 4.2:.1f}s infinite}}")
    css.append("@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}.blink{animation:blink 1s infinite}")
    css.append(
        "@keyframes drift{0%,100%{transform:translate(0,0)}50%{transform:translate(40px,-30px)}}"
        "@keyframes drift2{0%,100%{transform:translate(0,0)}50%{transform:translate(-50px,25px)}}"
        ".b1{animation:drift 14s ease-in-out infinite}.b2{animation:drift2 18s ease-in-out infinite}"
        "@keyframes pop{from{opacity:0;transform:translateY(24px) scale(.96)}to{opacity:1;transform:none}}"
        ".pop{transform-box:fill-box;transform-origin:center;animation:pop .8s cubic-bezier(.2,.9,.3,1.3) both}"
        "@keyframes line{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:none}}"
        ".ln{animation:line .35s ease-out both}"
        "@keyframes float{0%,100%{transform:translateY(0) rotate(2deg)}50%{transform:translateY(-10px) rotate(1deg)}}"
        ".win{transform-box:fill-box;transform-origin:center;animation:float 7s ease-in-out infinite}"
        "@keyframes rule{from{transform:scaleX(0)}to{transform:none}}"
        ".rule{transform-box:fill-box;transform-origin:left;animation:rule .9s .5s cubic-bezier(.2,.8,.2,1) both}"
    )

    # Name-led headline: a small mono greeting, the name, a subtitle, and an accent rule that draws in under them.
    name = hero_cfg.get("name", user["name"])
    title = (
        f'<text class="pop" style="animation-delay:.1s" x="{x0}" y="112" font-family="{MONO}" font-size="20"'
        f' fill="{p["accent_3"]}">{esc(hero_cfg["greeting"])}</text>'
        f'<text class="pop" style="animation-delay:.25s" x="{x0 - 3}" y="196" font-family="{SANS}" font-size="84"'
        f' font-weight="800" letter-spacing="-2" fill="url(#name)" filter="url(#shadow)">{esc(name)}</text>'
        f'<rect class="rule" x="{x0}" y="222" width="96" height="5" rx="2.5" fill="url(#rule)"/>'
        f'<text class="pop" style="animation-delay:.45s" x="{x0}" y="266" font-family="{SANS}" font-size="24"'
        f' font-weight="500" fill="#fff" fill-opacity=".88">{esc(hero_cfg["subtitle"])}</text>'
    )

    # neofetch-style window with live numbers
    top_langs = ", ".join(lang["name"] for lang in s["languages"][:3])
    years = (s["today"] - s["created"]).days // 365
    rows = [
        ("role", hero_cfg["role"]),
        ("school", hero_cfg["school"]),
        ("langs", top_langs.lower()),
        ("github", f"{years} yrs, {s['repos']} public repos"),
        ("commits", f"{s['year_commits']} in the last year"),
        ("streak", f"{s['current_streak']}d now, {s['longest_streak']}d best"),
        ("status", hero_cfg["status"]),
    ]
    wx, wy, ww, wh = 735, 58, 410, 290
    lines = [
        f'<text class="ln" style="animation-delay:1.1s" x="{wx + 22}" y="{wy + 62}" font-family="{MONO}" font-size="15"'
        f' font-weight="700" fill="{p["accent_3"]}">{esc(user["handle"])}</text>',
        f'<text class="ln" style="animation-delay:1.2s" x="{wx + 22}" y="{wy + 80}" font-family="{MONO}" font-size="15"'
        f' fill="{p["muted"]}">{"─" * len(user["handle"])}</text>',
    ]
    for i, (key, value) in enumerate(rows):
        lines.append(
            f'<text class="ln" style="animation-delay:{1.3 + i * 0.12:.2f}s" x="{wx + 22}" y="{wy + 106 + i * 22}"'
            f' font-family="{MONO}" font-size="14" fill="{p["fg"]}"><tspan fill="{p["accent_2"]}" font-weight="700">'
            f'{key}</tspan><tspan x="{wx + 22 + 9 * 14 * MONO_ADVANCE:.1f}">{esc(value)}</tspan></text>'
        )
    swatches = [p["accent"], p["accent_2"], p["accent_3"], "#3fb950", "#58a6ff", p["fg"]]
    lines += [
        f'<rect class="ln" style="animation-delay:{2.3 + i * 0.05:.2f}s" x="{wx + 22 + i * 26}" y="{wy + wh - 34}"'
        f' width="22" height="12" rx="3" fill="{c}"/>'
        for i, c in enumerate(swatches)
    ]
    window = (
        f'<g class="pop" style="animation-delay:.7s"><g class="win">'
        f'<rect x="{wx}" y="{wy}" width="{ww}" height="{wh}" rx="14" fill="{p["bg"]}" fill-opacity=".92"'
        f' stroke="#ffffff" stroke-opacity=".18" filter="url(#shadow)"/>'
        + "".join(f'<circle cx="{wx + 22 + i * 18}" cy="{wy + 22}" r="5.5" fill="{c}"/>' for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")))
        + f'<text x="{wx + ww / 2}" y="{wy + 27}" text-anchor="middle" font-family="{MONO}" font-size="12"'
        f' fill="{p["muted"]}">~/neofetch</text>'
        + "".join(lines)
        + "</g></g>"
    )

    body = (
        "<defs>"
        f'<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{p["accent"]}"/>'
        f'<stop offset="1" stop-color="{mix(p["accent"], "#000000", 0.45)}"/></linearGradient>'
        f'<linearGradient id="name" x1="0" y1="0" x2="0" y2="1"><stop offset=".35" stop-color="#fff"/>'
        f'<stop offset="1" stop-color="{mix("#ffffff", p["accent_2"], 0.3)}"/></linearGradient>'
        f'<linearGradient id="rule"><stop offset="0" stop-color="{p["accent_3"]}"/><stop offset="1" stop-color="{p["accent_2"]}"/></linearGradient>'
        '<filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="50"/></filter>'
        '<filter id="shadow" x="-10%" y="-10%" width="130%" height="140%">'
        '<feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#000" flood-opacity=".35"/></filter>'
        '<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">'
        '<circle cx="2" cy="2" r="1.2" fill="#fff" fill-opacity=".12"/></pattern>'
        f'<clipPath id="frame"><rect width="{w}" height="{h}" rx="22"/></clipPath>'
        + "".join(clips)
        + "</defs>"
        f'<g clip-path="url(#frame)"><rect width="{w}" height="{h}" fill="url(#bg)"/>'
        f'<g class="b1"><circle cx="980" cy="60" r="160" fill="{p["accent_2"]}" fill-opacity=".55" filter="url(#blur)"/></g>'
        f'<g class="b2"><circle cx="260" cy="380" r="170" fill="{p["accent_3"]}" fill-opacity=".35" filter="url(#blur)"/></g>'
        f'<rect width="{w}" height="{h}" fill="url(#dots)"/></g>'
        + title
        + f'<g class="pop" style="animation-delay:.6s"><text x="{x0}" y="{y0}" font-family="{MONO}" font-size="{font}"'
        f' fill="{p["accent_3"]}" textLength="{len(prompt) * char_w:.1f}" lengthAdjust="spacing">{esc(prompt)}</text>'
        + "".join(texts)
        + f'<g class="cur"><rect class="blink" x="{tx + 2:.1f}" y="{y0 - 22}" width="12" height="27" fill="#fff"/></g></g>'
        + window
    )
    return svg(w, h, body, "".join(css), f"{name}: {hero_cfg['subtitle']}")


def stats_card(cfg: dict, s: dict, theme: str) -> str:
    p = palette(cfg, theme)
    w, h = 495, 210
    metrics = [
        ("contributions", compact(s["total"]), "all time"),
        ("commits", compact(s["year_commits"]), "past year"),
        ("pull requests", compact(s["year_prs"]), "past year"),
        ("issues", compact(s["year_issues"]), "past year"),
        ("active days", compact(s["active_days"]), "past year"),
        ("followers", compact(s["followers"]), f"{s['repos']} public repos"),
    ]
    body = [card_frame(p, w, h, "github stats", "▲")]
    for i, (label, value, sub) in enumerate(metrics):
        col, row = i % 2, i // 2
        x, y = 24 + col * 145, 76 + row * 46
        body.append(
            f'<g class="in" style="animation-delay:{0.15 + i * 0.08:.2f}s">'
            f'<text x="{x}" y="{y}" font-family="{SANS}" font-size="22" font-weight="800" fill="{p["fg"]}">{value}</text>'
            f'<text x="{x}" y="{y + 16}" font-family="{SANS}" font-size="11" fill="{p["muted"]}">{label}'
            f' <tspan fill-opacity=".7">· {esc(sub)}</tspan></text></g>'
        )

    # streak ring: fills toward the longest streak
    cx, cy, r = 395, 112, 52
    circ = 2 * math.pi * r
    frac = s["current_streak"] / s["longest_streak"] if s["longest_streak"] else 0
    style = (
        f"@keyframes ring{{from{{stroke-dashoffset:{circ:.1f}}}to{{stroke-dashoffset:{circ * (1 - frac):.1f}}}}}"
        f".ring{{stroke-dasharray:{circ:.1f};stroke-dashoffset:{circ * (1 - frac):.1f};animation:ring 1.4s .4s cubic-bezier(.3,.8,.3,1) both}}"
        "@keyframes flick{0%,100%{transform:scale(1)}50%{transform:scale(1.15)}}"
        ".fire{transform-box:fill-box;transform-origin:bottom;animation:flick 1.6s ease-in-out infinite}"
    )
    body.append(
        f'<defs><linearGradient id="rg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{p["accent_2"]}"/>'
        f'<stop offset="1" stop-color="{p["accent"]}"/></linearGradient></defs>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{p["border"]}" stroke-width="9"/>'
        f'<circle class="ring" cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="url(#rg)" stroke-width="9"'
        f' stroke-linecap="round" transform="rotate(-90 {cx} {cy})"/>'
        f'<path class="fire" transform="translate({cx - 8} {cy - r - 26})" fill="url(#rg)"'
        ' d="M8 0C9 4 14 6 14 11.5A6 6 0 0 1 2 11.5C2 8.5 4 7 4.5 5C6 6.5 6 8 6 8C7.5 6 8 3.5 8 0Z"/>'
        f'<g class="in" style="animation-delay:.5s"><text x="{cx}" y="{cy + 8}" text-anchor="middle" font-family="{SANS}"'
        f' font-size="32" font-weight="800" fill="{p["fg"]}">{s["current_streak"]}</text>'
        f'<text x="{cx}" y="{cy + 26}" text-anchor="middle" font-family="{SANS}" font-size="11" fill="{p["muted"]}">day streak</text></g>'
        f'<text class="in" style="animation-delay:.7s" x="{cx}" y="{cy + r + 26}" text-anchor="middle" font-family="{SANS}"'
        f' font-size="11" fill="{p["muted"]}">best: <tspan fill="{p["accent_2"]}" font-weight="700">{s["longest_streak"]} days</tspan></text>'
    )
    return svg(w, h, "".join(body), style, "GitHub stats")


def languages_card(cfg: dict, s: dict, theme: str) -> str:
    p = palette(cfg, theme)
    w, h = 495, 210
    body = [card_frame(p, w, h, "languages", "◆")]
    bar_x, bar_y, bar_w = 24, 58, w - 48
    body.append(f'<defs><clipPath id="bar"><rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="10" rx="5"/></clipPath></defs>')
    body.append(f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="10" rx="5" fill="{p["border"]}"/>')
    style = (
        "@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}"
        f".grow{{transform-origin:{bar_x}px 0;animation:grow 1.2s .2s cubic-bezier(.3,.8,.3,1) both}}"
    )
    x = bar_x
    segs = []
    for lang in s["languages"]:
        seg = bar_w * lang["pct"] / 100
        segs.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{seg + 0.5:.1f}" height="10" fill="{lang["color"]}"/>')
        x += seg
    body.append(f'<g clip-path="url(#bar)"><g class="grow">{"".join(segs)}</g></g>')
    for i, lang in enumerate(s["languages"]):
        col, row = i % 2, i // 2
        lx, ly = 24 + col * 225, 98 + row * 27
        body.append(
            f'<g class="in" style="animation-delay:{0.6 + i * 0.07:.2f}s">'
            f'<circle cx="{lx + 6}" cy="{ly - 4}" r="6" fill="{lang["color"]}"/>'
            f'<text x="{lx + 20}" y="{ly}" font-family="{SANS}" font-size="13" fill="{p["fg"]}">{esc(lang["name"])}'
            f' <tspan fill="{p["muted"]}">{lang["pct"]:.1f}%</tspan></text></g>'
        )
    return svg(w, h, "".join(body), style, "Most used languages")


def activity_card(cfg: dict, s: dict, theme: str) -> str:
    p = palette(cfg, theme)
    weeks = s["weeks"][-cfg.get("cards", {}).get("activity", {}).get("weeks", 53) :]
    cell, gap = 13, 3
    left, top = 54, 70
    w = left + len(weeks) * (cell + gap) + 24
    h = top + 7 * (cell + gap) + 46
    counts = sorted(d["contributionCount"] for wk in weeks for d in wk["contributionDays"] if d["contributionCount"])
    # quartiles of the non-zero days, like GitHub's own levels
    cuts = [counts[int(len(counts) * q)] for q in (0.25, 0.5, 0.75)] if counts else [1, 2, 3]
    levels = [p["border"]] + [mix(p["card"], p["accent"], t) for t in (0.45, 0.7, 1.0)]
    levels.append(p["accent_2"])

    def level(n: int) -> int:
        return 0 if n == 0 else 1 + sum(n > c for c in cuts)

    total = s["year_total"]
    body = [card_frame(p, w, h, f"{total} contributions in the last year", "●")]
    body.append(
        f'<text class="in" x="{w - 24}" y="38" text-anchor="end" font-family="{SANS}" font-size="12" fill="{p["muted"]}">'
        f'{s["current_streak"]}d streak · best {s["longest_streak"]}d</text>'
    )
    style = (
        "@keyframes cell{from{opacity:0;transform:scale(.2)}to{opacity:1;transform:none}}"
        ".c{transform-box:fill-box;transform-origin:center;animation:cell .5s ease-out both}"
        "@keyframes glow{0%,100%{opacity:1}50%{opacity:.55}}"
        ".hot{animation:cell .5s ease-out both,glow 2.4s 2s ease-in-out infinite}"
    )
    for i, label in ((1, "mon"), (3, "wed"), (5, "fri")):
        body.append(
            f'<text x="{left - 10}" y="{top + i * (cell + gap) + 10}" text-anchor="end" font-family="{SANS}"'
            f' font-size="10" fill="{p["muted"]}">{label}</text>'
        )
    last_month = None
    for wi, week in enumerate(weeks):
        x = left + wi * (cell + gap)
        first = week["contributionDays"][0]["date"]
        month = first[5:7]
        if month != last_month and wi < len(weeks) - 2:
            name = dt.date.fromisoformat(first).strftime("%b").lower()
            body.append(f'<text x="{x}" y="{top - 10}" font-family="{SANS}" font-size="10" fill="{p["muted"]}">{name}</text>')
            last_month = month
        for day in week["contributionDays"]:
            lv = level(day["contributionCount"])
            y = top + day["weekday"] * (cell + gap)
            cls = "c hot" if lv == 4 else "c"
            body.append(
                f'<rect class="{cls}" style="animation-delay:{0.2 + wi * 0.025 + day["weekday"] * 0.02:.3f}s" x="{x}" y="{y}"'
                f' width="{cell}" height="{cell}" rx="3" fill="{levels[lv]}"><title>{day["contributionCount"]} on {day["date"]}</title></rect>'
            )
    ly = h - 26
    body.append(f'<text x="{w - 24 - 5 * (cell + gap) - 8}" y="{ly + 10}" text-anchor="end" font-family="{SANS}" font-size="10" fill="{p["muted"]}">less</text>')
    for i, c in enumerate(levels):
        body.append(f'<rect x="{w - 24 - (5 - i) * (cell + gap)}" y="{ly}" width="{cell}" height="{cell}" rx="3" fill="{c}"/>')
    body.append(f'<text x="{left}" y="{ly + 10}" font-family="{SANS}" font-size="10" fill="{p["muted"]}">'
                f'updated {s["today"].isoformat()}</text>')
    return svg(w, h, "".join(body), style, "Contribution activity")


def project_card(cfg: dict, s: dict, item: dict, theme: str) -> str:
    p = palette(cfg, theme)
    repo = s["featured"][item["repo"]]
    w, h = 495, 128
    desc = wrap(item.get("blurb") or repo["description"] or "no description, just vibes.", 66, 2)
    lang = repo["primaryLanguage"] or {"name": "—", "color": p["muted"]}
    body = [
        f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="12" fill="{p["card"]}" stroke="{p["border"]}"/>',
        f'<rect class="edge" x="0" y="14" width="4" height="{h - 28}" rx="2" fill="{lang["color"] or p["accent"]}"/>',
        f'<g class="in"><path transform="translate(24 21) scale(1)" fill="{p["muted"]}" d="M2 2.5A2.5 2.5 0 0 1 4.5 0h8.75a.75.75 0 0 1 .75.75v12.5a.75.75 0 0 1-.75.75h-2.5a.75.75 0 0 1 0-1.5h1.75v-2h-8a1 1 0 0 0-.714 1.7.75.75 0 1 1-1.072 1.05A2.495 2.495 0 0 1 2 11.5Zm10.5-1h-8a1 1 0 0 0-1 1v6.708A2.486 2.486 0 0 1 4.5 9h8ZM5 12.25a.25.25 0 0 1 .25-.25h3.5a.25.25 0 0 1 .25.25v3.25a.25.25 0 0 1-.4.2l-1.45-1.087a.25.25 0 0 0-.3 0L5.4 15.7a.25.25 0 0 1-.4-.2Z"/>',
        f'<text x="48" y="34" font-family="{SANS}" font-size="15" font-weight="700" fill="{p["accent_2"] if theme == "dark" else p["accent"]}">'
        f'{esc(repo["name"])}</text>',
        f'<text x="{w - 24}" y="34" text-anchor="end" font-family="{SANS}" font-size="11" fill="{p["muted"]}">'
        f'{"archived · " if repo["isArchived"] else ""}pushed {ago(repo["pushedAt"], s["today"])}</text></g>',
    ]
    for i, line in enumerate(desc):
        body.append(
            f'<text class="in" style="animation-delay:{0.1 + i * 0.08:.2f}s" x="24" y="{60 + i * 18}" font-family="{SANS}"'
            f' font-size="13" fill="{p["fg"]}">{esc(line)}</text>'
        )
    body.append(
        f'<g class="in" style="animation-delay:.3s"><circle cx="30" cy="{h - 24}" r="6" fill="{lang["color"] or p["muted"]}"/>'
        f'<text x="42" y="{h - 20}" font-family="{SANS}" font-size="12" fill="{p["muted"]}">{esc(lang["name"])}'
        + (f'    ★ {compact(repo["stargazerCount"])}' if repo["stargazerCount"] else "")
        + (f'    ⑂ {compact(repo["forkCount"])}' if repo["forkCount"] else "")
        + "</text></g>"
    )
    style = (
        "@keyframes edge{from{transform:scaleY(0)}to{transform:scaleY(1)}}"
        f".edge{{transform-origin:0 {h / 2}px;animation:edge .8s cubic-bezier(.3,.8,.3,1)}}"
    )
    return svg(w, h, "".join(body), style, repo["name"])


def experience_card(cfg: dict, s: dict, theme: str) -> str:
    p = palette(cfg, theme)
    items = cfg["experience"]
    w, row = 1000, 80
    h = 72 + len(items) * row
    date_x, line_x, text_x = 24, 196, 224
    kinds = {"work": p["accent"], "lead": p["accent_2"], "school": p["accent_3"]}
    body = [card_frame(p, w, h, "experience", "◇")]
    style = (
        "@keyframes draw{from{transform:scaleY(0)}to{transform:scaleY(1)}}"
        f".rail{{transform-origin:0 64px;animation:draw 1.4s .1s cubic-bezier(.3,.8,.3,1) both}}"
        "@keyframes ping{0%{transform:scale(1);opacity:.7}80%,100%{transform:scale(2.6);opacity:0}}"
        ".ping{transform-box:fill-box;transform-origin:center;animation:ping 2s ease-out infinite}"
    )
    body.append(
        f'<rect class="rail" x="{line_x - 1}" y="64" width="2" height="{h - 64 - row / 2 - 4:.0f}" rx="1" fill="{p["border"]}"/>'
    )
    for i, item in enumerate(items):
        y = 72 + i * row
        color = kinds.get(item.get("kind", "work"), p["accent"])
        current = item["dates"].lower().endswith("present")
        delay = 0.25 + i * 0.12
        dot = f'<circle cx="{line_x}" cy="{y + 12}" r="6" fill="{color}" stroke="{p["card"]}" stroke-width="3"/>'
        if current:
            dot = f'<circle class="ping" cx="{line_x}" cy="{y + 12}" r="6" fill="{color}"/>' + dot
        lines = wrap(item["blurb"], 112, 2)
        body.append(
            f'<g class="in" style="animation-delay:{delay:.2f}s">'
            f'<text x="{date_x}" y="{y + 16}" font-family="{MONO}" font-size="12" fill="{p["muted"]}">{esc(item["dates"].lower())}</text>'
            + (
                f'<rect x="{date_x}" y="{y + 26}" width="38" height="17" rx="8.5" fill="{color}" fill-opacity=".18"/>'
                f'<text x="{date_x + 19}" y="{y + 38}" text-anchor="middle" font-family="{SANS}" font-size="10" font-weight="700"'
                f' fill="{color}">now</text>'
                if current
                else ""
            )
            + dot
            + f'<text x="{text_x}" y="{y + 17}" font-family="{SANS}" font-size="15" font-weight="700" fill="{p["fg"]}">'
            f'{esc(item["org"])} <tspan font-weight="400" fill="{p["accent_2"] if theme == "dark" else p["accent"]}">· {esc(item["role"])}</tspan></text>'
            + "".join(
                f'<text x="{text_x}" y="{y + 38 + j * 17}" font-family="{SANS}" font-size="12.5" fill="{p["muted"]}">{esc(line)}</text>'
                for j, line in enumerate(lines)
            )
            + "</g>"
        )
    return svg(w, h, "".join(body), style, "Experience timeline")


def slug(repo: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")


def build_cards(cfg: dict, data: dict, out: Path) -> list[str]:
    s = summarize(cfg, data)
    files = {"hero.svg": hero(cfg, s)}
    for theme in THEMES:
        files[f"stats-{theme}.svg"] = stats_card(cfg, s, theme)
        files[f"languages-{theme}.svg"] = languages_card(cfg, s, theme)
        files[f"activity-{theme}.svg"] = activity_card(cfg, s, theme)
        if cfg.get("experience"):
            files[f"experience-{theme}.svg"] = experience_card(cfg, s, theme)
        for item in cfg.get("featured", []):
            repo = s["featured"].get(item["repo"])
            if repo and not repo["isPrivate"]:
                files[f"project-{slug(item['repo'])}-{theme}.svg"] = project_card(cfg, s, item, theme)
            else:
                # a local token can see private repos; never put them on a public card
                print(f"warning: featured repo {item['repo']} is missing or private, skipping", file=sys.stderr)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.svg"):  # drop cards for repos that were unfeatured or went private
        if old.name not in files and not old.name.startswith("snake-"):
            old.unlink()
    for name, content in files.items():
        (out / name).write_text(content)
    return sorted(files)


# --------------------------------------------------------------------------- readme


def picture(base: str, name: str, alt: str, width: str, href: str | None = None) -> str:
    pic = (
        f'<picture><source media="(prefers-color-scheme: dark)" srcset="{base}{name}-dark.svg">'
        f'<source media="(prefers-color-scheme: light)" srcset="{base}{name}-light.svg">'
        f'<img alt="{esc(alt)}" src="{base}{name}-dark.svg" width="{width}"></picture>'
    )
    return f'<a href="{esc(href)}">{pic}</a>' if href else pic


def badge(label: str, logo: str, color: str, style: str = "for-the-badge") -> str:
    from urllib.parse import quote

    text = quote(label.replace("-", "--").replace("_", "__"))
    logo_args = f"&logo={logo}&logoColor=white" if logo else ""
    return f"https://img.shields.io/badge/{text}-{color}?style={style}{logo_args}"


def render_readme(cfg: dict, base: str) -> str:
    user, login = cfg["user"], cfg["user"]["login"]
    profile_url = f"https://github.com/{login}"
    blocks = {}
    blocks["hero"] = f'<a href="{esc(user["website"])}"><img alt="{esc(user["name"])}" src="{base}hero.svg" width="100%"></a>'
    blocks["links"] = "\n".join(
        f'<a href="{esc(link["url"])}"><img alt="{esc(link["label"])}" src="{badge(link["label"], link["logo"], link["color"])}"></a>'
        for link in cfg.get("links", [])
    ) + (
        f'\n<a href="{profile_url}?tab=followers"><img alt="followers" src="https://img.shields.io/github/followers/{login}'
        f'?style=for-the-badge&logo=github&label=follow&color=161b22"></a>'
    )
    now = cfg.get("now", {})
    blocks["whoami"] = "\n".join(
        [
            "$ whoami",
            f"{user['name'].lower()}, {user['location'].lower()}",
            "",
            "$ cat ~/.now",
        ]
        + [f"{key.replace('_', ' ')}: {value}" for key, value in now.items()]
    )
    blocks["stats"] = (
        picture(base, "stats", "github stats", "49%", f"{profile_url}?tab=repositories")
        + "\n"
        + picture(base, "languages", "top languages", "49%", f"{profile_url}?tab=repositories")
    )
    blocks["activity"] = picture(base, "activity", "contribution activity", "100%", profile_url)
    blocks["experience"] = picture(base, "experience", "experience timeline", "100%", user.get("linkedin"))
    blocks["highlights"] = "\n".join(f"- {h}" for h in cfg.get("timeline", {}).get("highlights", []))
    cards = [
        picture(base, f"project-{slug(item['repo'])}", item["repo"], "49%", f"https://github.com/{item['repo'] if '/' in item['repo'] else f'{login}/{item['repo']}'}")
        for item in cfg.get("featured", [])
    ]
    blocks["featured"] = "\n".join(" ".join(cards[i : i + 2]) for i in range(0, len(cards), 2))
    stack_rows = []
    for group, items in cfg.get("stack", {}).items():
        imgs = " ".join(
            (f'<a href="{esc(i["url"])}">{{}}</a>' if i.get("url") else "{}").format(
                f'<img alt="{esc(i["name"])}" src="{badge(i["name"], i.get("logo", ""), i["color"], "flat-square")}">'
            )
            for i in items
        )
        stack_rows.append(f"<b>{esc(group.replace('_', ' '))}</b><br/>\n{imgs}")
    blocks["stack"] = "\n\n".join(f"<p>{row}</p>" for row in stack_rows)
    blocks["snake"] = (
        picture(base, "snake", "snake eating my contribution graph", "100%")
        if cfg.get("cards", {}).get("snake", {}).get("enabled")
        else ""
    )
    text = TEMPLATE.read_text()
    for key, value in blocks.items():
        text = text.replace("{{" + key + "}}", value)
    if leftover := re.findall(r"\{\{\w+\}\}", text):
        sys.exit(f"template placeholders without a value: {leftover}")
    return re.sub(r"\n{3,}", "\n\n", text)


def raw_base(cfg: dict) -> str:
    login = cfg["user"]["login"]
    return f"https://raw.githubusercontent.com/{login}/{login}/output/"


def write_preview(cfg: dict, out: Path) -> Path:
    readme = render_readme(cfg, "./")
    # just enough markdown for eyeballing: code fences and headings; the rest is html already
    body = re.sub(r"```console\n(.*?)```", lambda m: f"<pre>{esc(m.group(1))}</pre>", readme, flags=re.S)
    body = re.sub(r"^## (.*)$", r"<h2>\1</h2>", body, flags=re.M)
    body = re.sub(r"^- (.*)$", r"<li>\1</li>", body, flags=re.M)
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
    body = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', body)
    page = (
        "<!doctype html><meta charset=utf-8><title>profile preview</title>"
        "<style>body{background:#0d1117;color:#e6edf3;font:16px -apple-system,sans-serif;max-width:1012px;margin:24px auto;padding:0 16px}"
        "pre{background:#161b22;padding:16px;border-radius:6px}h2{border-bottom:1px solid #30363d;padding-bottom:6px}"
        "img{vertical-align:top}@media(prefers-color-scheme:light){body{background:#fff;color:#1f2328}pre{background:#f6f8fa}}</style>"
        + body
    )
    path = out / "preview.html"
    path.write_text(page)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--readme", action="store_true", help="render README.md and exit")
    ap.add_argument("--check", action="store_true", help="fail if README.md is stale")
    ap.add_argument("--cached", action="store_true", help="use dist/data.json instead of the API")
    ap.add_argument("--preview", action="store_true", help="also write dist/preview.html")
    ap.add_argument("--out", type=Path, default=DIST)
    args = ap.parse_args()
    cfg = tomllib.loads(CONFIG.read_text())

    if args.readme or args.check:
        rendered = render_readme(cfg, raw_base(cfg))
        if args.check:
            if README.read_text() != rendered:
                sys.exit("README.md is out of date: run `python scripts/generate.py --readme` and commit it")
            print("README.md is up to date")
        else:
            README.write_text(rendered)
            print("wrote README.md")
        return

    cache = args.out / "data.json"
    if args.cached:
        data = json.loads(cache.read_text())
    else:
        data = fetch(cfg)
        args.out.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, indent=1))
    for name in build_cards(cfg, data, args.out):
        print(f"wrote {args.out.name}/{name}")
    if args.preview:
        print(f"wrote {write_preview(cfg, args.out)}")


if __name__ == "__main__":
    main()
