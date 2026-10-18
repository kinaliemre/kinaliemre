"""Build the whole profile (README.md + assets/*.svg) from live data.

Data sources, in order of preference:
  * PROFILE_TOKEN (a personal access token of the profile owner): the REST API,
    reading the commit history of every repo I can access, private and team repos too.
  * no token: GitHub's public commit search (public repos only).
  * --local PATH ...: also read commits straight from local git repos
    (used for previews on my own machine).

Everything else (descriptions, skills) comes from profile.json.
Standard library only.
"""
import argparse
import calendar
import collections
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CFG = json.load(open(os.path.join(ROOT, "profile.json"), encoding="utf-8"))
USER = CFG["user"]
OWNERS = {USER.lower(), *(a.lower() for a in CFG.get("aliases", []))}
TZ = dt.timezone(dt.timedelta(hours=CFG.get("utc_offset_hours", 0)))
TOKEN = os.environ.get("PROFILE_TOKEN") or os.environ.get("GITHUB_TOKEN")
OWNER_TOKEN = bool(os.environ.get("PROFILE_TOKEN"))

# ---------------------------------------------------------------- GitHub API

def api(url, data=None):
    req = urllib.request.Request(url, data=data, headers={
        "Accept": "application/vnd.github+json", "User-Agent": USER})
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def rest(path, **params):
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    return api(f"https://api.github.com/{path}{q}")


def repo_public(full):
    """True/False, or None when GitHub would not say (rate limit)."""
    try:
        return not rest(f"repos/{full}").get("private", True)
    except urllib.error.HTTPError as err:
        return False if err.code == 404 else None


def to_local_day(stamp):
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(TZ).date()

# ---------------------------------------------------------------- commit events
# event = (day, "owner/repo", count, sha_or_None)

def events_rest(since, today):
    """Every commit I authored, in every repo I can see (own, collaborator, org).
    Uses the real commit history, so it matches the repos' commit pages."""
    out, private, page = [], {}, 1
    repos = []
    while True:
        batch = rest("user/repos", affiliation="owner,collaborator,organization_member", per_page=100, page=page)
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    for repo in repos:
        full = repo["full_name"]
        page = 1
        while True:
            try:
                batch = rest(f"repos/{full}/commits", author=USER, since=f"{since}T00:00:00Z", per_page=100, page=page)
            except urllib.error.HTTPError:
                break                      # empty repo or no access to history
            for c in batch:
                out.append((to_local_day(c["commit"]["committer"]["date"]), full, 1, c["sha"]))
            if len(batch) < 100:
                break
            page += 1
        if any(e[1] == full for e in out):
            private[full] = repo["private"]
    return out, private


def events_search(since, today):
    out, page = [], 1
    while True:
        data = rest("search/commits", q=f"author:{USER} committer-date:{since}..{today}", per_page=100, page=page)
        for it in data.get("items", []):
            out.append((to_local_day(it["commit"]["committer"]["date"]), it["repository"]["full_name"], 1, it["sha"]))
        if page * 100 >= min(data.get("total_count", 0), 1000):
            return out
        page += 1


def events_local(paths, since):
    out = []
    for path in paths:
        url = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        if "github.com" not in url:
            continue
        full = url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
        args = ["git", "-C", path, "log", "--all", f"--since={since}", "--format=%H|%cI"]
        args += [f"--author={e}" for e in CFG["author_emails"]]
        log = subprocess.run(args, capture_output=True, text=True, encoding="utf-8").stdout
        for line in log.splitlines():
            sha, stamp = line.split("|", 1)
            out.append((to_local_day(stamp), full, 1, sha))
    return out


FILE_VISIBILITY = {}


def events_file(path):
    """Events from a file of 'owner/repo|YYYY-MM-DD|count[|latest sha[|public/private]]' lines."""
    out = []
    for line in open(path, encoding="utf-8"):
        if line.strip():
            repo, day, n, *rest_ = line.strip().split("|")
            out.append((dt.date.fromisoformat(day), repo, int(n), rest_[0] if rest_ else None))
            if len(rest_) > 1:
                FILE_VISIBILITY[repo] = rest_[1] == "private"
    return out


def collect(local_paths, extra_files=()):
    today = dt.datetime.now(TZ).date()
    since = dt.date.fromisoformat(CFG["since"])
    private, found_by_search = {}, set()
    if OWNER_TOKEN:
        events, private = events_rest(since, today)
    else:
        try:
            events = events_search(since, today)
        except urllib.error.HTTPError as err:
            if not extra_files:
                raise
            print(f"search: {err}, using the event files only")
            events = []
        found_by_search = {e[1] for e in events}   # public search only returns public repos
    if local_paths:
        seen = {e[3] for e in events if e[3]}
        events += [e for e in events_local(local_paths, since) if e[3] not in seen]
    for path in extra_files:            # a file is the authority for the repos it lists
        listed = events_file(path)
        repos_in_file = {e[1] for e in listed}
        events = [e for e in events if e[1] not in repos_in_file] + listed
    excluded = {r.lower() for r in CFG.get("exclude_repos", [])}
    events = [e for e in events if e[1].lower() not in excluded and since <= e[0] <= today]
    # commits from repos that no longer exist on GitHub (kept in profile.json)
    known = {e[1] for e in events}
    for a in CFG.get("archived_commits", []):
        if a["repo"] not in known:
            events.append((dt.date.fromisoformat(a["date"]), a["repo"], a["count"], a.get("sha")))
        private.setdefault(a["repo"], a.get("private", True))
    private.update(FILE_VISIBILITY)
    for repo in {e[1] for e in events}:
        if repo in private:
            continue
        if repo in found_by_search:
            private[repo] = False
        else:
            public = repo_public(repo)
            private[repo] = True if public is None else not public
    return today, events, private

# ---------------------------------------------------------------- drawing helpers
SANS = "-apple-system, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, 'SFMono-Regular', Consolas, 'Liberation Mono', monospace"
TILE, STROKE, EMPTY = "#161b22", "#30363d", "#21262d"
TXT, SUB, DIM, FAINT = "#e6edf3", "#c9d1d9", "#8b949e", "#6e7681"
GREEN, GREEN_DARK = "#3fb950", "#2ea043"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def t(x, y, s, fill, size=14, weight=400, anchor="start", font=SANS):
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" xml:space="preserve" '
            f'style="font-family:{font};font-size:{size}px;font-weight:{weight}" fill="{fill}">{esc(s)}</text>')


def tile(x, y, w, h, stroke=STROKE):
    return f'<rect x="{x + .5}" y="{y + .5}" width="{w - 1}" height="{h - 1}" rx="8" fill="{TILE}" stroke="{stroke}"/>'


# GitHub light theme counterparts of the dark palette (language colours stay as they are)
LIGHT = {
    "#0d1117": "#ffffff", TILE: "#f6f8fa", STROKE: "#d0d7de", EMPTY: "#ebedf0",
    TXT: "#1f2328", SUB: "#424a53", DIM: "#59636e", FAINT: "#818b98",
    GREEN: "#1a7f37", GREEN_DARK: "#1f883d", "#238636": "#1f883d",
    "#d29922": "#9a6700", "#3b2e0a": "#f5e0a8",
    "#0e4429": "#9be9a8", "#006d32": "#40c463", "#26a641": "#30a14e", "#39d353": "#216e39",
}


def write_svg(name, w, h, aria, body):
    """Write the card twice: name.svg (dark theme) and name-light.svg (light theme)."""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
           f'role="img" aria-label="{esc(aria)}">\n  ' + "\n  ".join(body) + "\n</svg>\n")
    light = re.sub(r"#[0-9a-fA-F]{6}\b", lambda m: LIGHT.get(m.group(0).lower(), m.group(0)), svg)
    for file_name, data in ((name, svg), (name.replace(".svg", "-light.svg"), light)):
        with open(os.path.join(ROOT, "assets", file_name), "w", encoding="utf-8", newline="\n") as f:
            f.write(data)


def themed_img(name, alt):
    """<picture> that GitHub switches with the viewer's light/dark theme."""
    return (f'<picture>\n  <source media="(prefers-color-scheme: dark)" srcset="assets/{name}.svg"/>\n'
            f'  <source media="(prefers-color-scheme: light)" srcset="assets/{name}-light.svg"/>\n'
            f'  <img src="assets/{name}.svg" alt="{alt}" width="100%"/>\n</picture>')


def shade(n):
    return EMPTY if n == 0 else "#0e4429" if n <= 2 else "#006d32" if n <= 4 else "#26a641" if n <= 8 else "#39d353"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def month_label(y, m, full=False):
    return f"{(calendar.month_name if full else calendar.month_abbr)[m]} {y}"


def title_of(repo):
    return CFG["projects"].get(repo, {}).get("title", repo.split("/")[1])


def is_own(repo):
    return repo.split("/")[0].lower() in OWNERS


def repo_key(repo):
    """Hidden repos are stored as short hashes, so profile.json doesn't reveal their names."""
    return hashlib.sha256(repo.lower().encode()).hexdigest()[:16]


def shown(repo):
    """Repos listed in History and counted as projects (homework etc. can be hidden)."""
    return repo_key(repo) not in set(CFG.get("hide_from_history", []))

# ---------------------------------------------------------------- sections

def build_hero(today, events):
    total = sum(e[2] for e in events)
    first = min(e[0] for e in events)
    months, (y, m) = [], (first.year, first.month)
    while (y, m) <= (today.year, today.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    per_month = collections.Counter()
    for d, _, n, _ in events:
        per_month[(d.year, d.month)] += n
    seq = [per_month[k] for k in months]
    repos = {e[1] for e in events if shown(e[1])}
    extras = CFG.get("extra_projects", [])
    own = sum(1 for r in repos if is_own(r)) + len(extras)
    n_projects = len(repos) + len(extras)

    H = 300
    b = [tile(0, 0, 540, H),
         t(32, 66, CFG["name"], TXT, 40, 700),
         t(32, 100, CFG["subtitle"], SUB, 15.5),
         f'<line x1="32" y1="128" x2="508" y2="128" stroke="{STROKE}"/>']
    b += [t(32, 162 + i * 24, line, DIM, 14.5) for i, line in enumerate(CFG["about"])]
    b += [t(32, 266, "Open to internships", GREEN, 13.5, 600), t(196, 266, CFG["email"], DIM, 13.5)]

    b += [tile(560, 0, 280, 196), t(582, 32, "Commits", DIM, 12, 600),
          t(582, 70, str(total), TXT, 32, 700),
          t(582 + len(str(total)) * 20 + 10, 70, f"since {month_label(first.year, first.month)}", DIM, 13)]
    mx, step = max(seq), (818 - 582) / len(seq)
    for i, v in enumerate(seq):
        h = max(2, 70 * v / mx) if v else 2
        b.append(f'<rect x="{582 + i * step:.1f}" y="{160 - h:.1f}" width="{max(2, step - 4):.1f}" '
                 f'height="{h:.1f}" rx="2" fill="{GREEN_DARK if v else EMPTY}"/>')
    b += [t(582, 180, month_label(*months[0]), FAINT, 11), t(818, 180, month_label(*months[-1]), FAINT, 11, anchor="end")]

    b += [tile(560, 212, 280, 88), t(582, 242, "Projects", DIM, 12, 600),
          t(582, 278, str(n_projects), TXT, 26, 700),
          t(582 + len(str(n_projects)) * 16 + 8, 278, f"{own} on my own, {n_projects - own} with a team", SUB, 13.5)]
    write_svg("hero.svg", 840, H, f"{CFG['name']} — {CFG['subtitle']}. {total} commits since "
              f"{month_label(first.year, first.month)}, {n_projects} projects. Open to internships.", b)
    return total, first


def build_month(today, events):
    ndays = calendar.monthrange(today.year, today.month)[1]
    per_day, per_repo = collections.Counter(), collections.Counter()
    for d, repo, n, _ in events:
        if (d.year, d.month) == (today.year, today.month):
            per_day[d.day] += n
            if shown(repo):              # hidden repos count, but are never named
                per_repo[repo] += n
    total = sum(per_day.values())
    if total:
        peak = max(per_day.values())
        peak_day = min(k for k, v in per_day.items() if v == peak)
        top = [title_of(r) for r, _ in per_repo.most_common(2)]
        note = (f"{plural(total, 'commit')} on {plural(len(per_day), 'day')} so far. "
                f"Busiest day: {calendar.month_abbr[today.month]} {peak_day} ({peak})."
                + (f" Mostly on {' and '.join(top)}." if top else ""))
    else:
        last = max(events, key=lambda e: e[0])
        where = f" ({title_of(last[1])})" if shown(last[1]) else ""
        note = f"No commits yet this month. The last one was on {last[0]:%b} {last[0].day}{where}."

    W, H, sq, gap = 840, 150, 18, 6
    x0 = (W - (ndays * (sq + gap) - gap)) / 2
    b = [tile(0, 0, W, H), t(x0, 36, month_label(today.year, today.month, True), TXT, 15, 600),
         t(W - x0, 36, f"{plural(total, 'commit')} · {plural(len(per_day), 'active day')}", DIM, 13, anchor="end")]
    for day in range(1, ndays + 1):
        x, n = x0 + (day - 1) * (sq + gap), per_day.get(day, 0)
        if day > today.day:
            b.append(f'<rect x="{x + .5}" y="56.5" width="{sq - 1}" height="{sq - 1}" rx="3" fill="none" stroke="{EMPTY}"/>')
        else:
            b.append(f'<rect x="{x}" y="56" width="{sq}" height="{sq}" rx="3" fill="{shade(n)}"/>')
        if day == today.day:
            b.append(f'<rect x="{x - 2.5}" y="53.5" width="{sq + 5}" height="{sq + 5}" rx="5" fill="none" stroke="{DIM}" stroke-width="1.5"/>')
        if n:
            b.append(t(x + sq / 2, 96, str(n), SUB, 11, 600, anchor="middle", font=MONO))
        elif day in (1, ndays) or day == today.day:
            b.append(t(x + sq / 2, 96, str(day), SUB if day == today.day else FAINT, 11, anchor="middle", font=MONO))
    b += [f'<line x1="{x0}" y1="110" x2="{W - x0}" y2="110" stroke="{STROKE}"/>',
          t(x0, 133, note, SUB, 13.5),
          t(W - x0, 133, f"updated {today:%b} {today.day}", FAINT, 11.5, anchor="end")]
    write_svg("activity.svg", W, H, f"{month_label(today.year, today.month, True)}: {note}", b)


AMBER = "#d29922"


def status_icon(cx, cy, active, r=8):
    """Animated status mark (SMIL, so it also moves inside GitHub's <img>).
    In progress: an amber loading spinner. Completed: a green check that draws itself."""
    if active:
        arc = 2 * 3.1416 * (r - 1)
        return [f'<circle cx="{cx}" cy="{cy}" r="{r - 1}" fill="none" stroke="#3b2e0a" stroke-width="2.4"/>',
                f'<circle cx="{cx}" cy="{cy}" r="{r - 1}" fill="none" stroke="{AMBER}" stroke-width="2.4" '
                f'stroke-linecap="round" stroke-dasharray="{arc * .3:.1f} {arc:.1f}">'
                f'<animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy}" '
                f'to="360 {cx} {cy}" dur="0.9s" repeatCount="indefinite"/></circle>']
    return [f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#238636">'
            f'<animate attributeName="r" values="{r};{r + 1.2};{r};{r}" keyTimes="0;0.12;0.25;1" dur="3s" repeatCount="indefinite"/></circle>',
            f'<path d="M{cx - 3.6} {cy + .2}l2.5 2.6 4.9-5.4" fill="none" stroke="#fff" stroke-width="1.9" '
            f'stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="12" stroke-dashoffset="12">'
            f'<animate attributeName="stroke-dashoffset" values="12;0;0" keyTimes="0;0.2;1" dur="3s" repeatCount="indefinite"/></path>']


def is_active(repo, last_day, today):
    """A project is in progress if profile.json says so, else if I committed to it recently."""
    status = CFG["projects"].get(repo, {}).get("status")
    if status:
        return status != "done"
    return (today - last_day).days <= CFG.get("active_days", 30)


def build_history(events, private, today):
    last, first, last_sha, sha_day = {}, {}, {}, {}
    # sorted() is stable and every source lists commits newest first, so within one
    # day the first sha we meet is that day's latest commit
    for d, repo, _, sha in sorted(events, key=lambda e: e[0]):
        first.setdefault(repo, d)
        last[repo] = d
        if sha and sha_day.get(repo) != d:
            last_sha[repo], sha_day[repo] = sha, d
    repos = sorted((r for r in last if shown(r)), key=lambda r: (last[r], first[r]), reverse=True)
    now = CFG.get("now")
    dated = []
    for r in repos:
        p = CFG["projects"].get(r, {})
        badge = ("team project · " if not is_own(r) else "") + ("private" if private.get(r) else "public")
        sha = None if private.get(r) else (last_sha.get(r) or "")[:7] or None
        dated.append((last[r], (f"{last[r]:%b %Y}", p.get("title", r.split("/")[1]), p.get("desc", ""),
                                " · ".join(p.get("tags", [])), badge, sha, is_active(r, last[r], today))))
    dated.sort(key=lambda kv: kv[0], reverse=True)
    rows = ([("now", now["title"], now["desc"], " · ".join(now["tags"]), "", None, True)] if now else [])
    rows += [row for _, row in dated]

    TH, GAP, TX, LX, TOP = 92, 10, 132, 108, 0
    HH = TOP + len(rows) * (TH + GAP) - GAP
    h = [f'<line x1="{LX}" y1="{TOP + TH / 2}" x2="{LX}" y2="{HH - TH / 2}" stroke="{STROKE}" stroke-width="2"/>']
    for i, (date, title, desc, tags, badge, sha, active) in enumerate(rows):
        y0, now_row = TOP + i * (TH + GAP), date == "now"
        cy = y0 + TH / 2
        h.append(t(LX - 18, cy + 4, date, SUB if now_row else DIM, 12, 500, anchor="end", font=MONO))
        h.append(f'<circle cx="{LX}" cy="{cy}" r="5" fill="{DIM}" stroke="#0d1117" stroke-width="2"/>')
        h += [tile(TX, y0, 840 - TX, TH, stroke=AMBER if now_row else STROKE),
              t(TX + 20, y0 + 30, title, TXT, 15.5, 600)]
        if badge:
            team, _, vis = badge.rpartition(" · ")
            col = GREEN if vis == "public" else DIM
            pw = len(vis) * 7 + 18
            h += [f'<rect x="{820 - pw}" y="{y0 + 16}" width="{pw}" height="20" rx="10" fill="none" stroke="{col}"/>',
                  t(820 - pw / 2, y0 + 30, vis, col, 11.5, 600, anchor="middle")]
            if team:
                h.append(t(820 - pw - 10, y0 + 30, team, DIM, 12, 500, anchor="end"))
        else:
            h.append(t(820, y0 + 30, "in progress", AMBER, 12, 600, anchor="end"))
        # status under the public/private label
        label, col = ("in progress", AMBER) if active else ("completed", GREEN)
        h += status_icon(820 - len(label) * 6.6 - 14, y0 + 52, active, 7)
        h.append(t(820, y0 + 56.5, label, col, 12, 600, anchor="end"))
        if sha:
            h.append(t(820, y0 + 78, sha, FAINT, 12, anchor="end", font=MONO))
        if desc:
            h.append(t(TX + 20, y0 + 53, desc, DIM, 13.5))
        if tags:
            h.append(t(TX + 20, y0 + 76, tags, FAINT, 12, font=MONO) if desc else
                     t(TX + 20, y0 + 56, tags, DIM, 12.5, font=MONO))   # no description: tags move up
    write_svg("history.svg", 840, HH, "Project history: " + ", ".join(
        f"{r[1]} ({'in progress' if r[6] else 'completed'})" for r in rows), h)
    return [r for r in repos if not private.get(r)]


LANG_COLORS = {"Python": "#3572A5", "HTML / CSS": "#e34c26", "JavaScript": "#f1e05a", "Java": "#b07219",
               "TypeScript": "#3178c6", "C++": "#f34b7d", "C": "#555555", "Jupyter Notebook": "#DA5B0B"}


LANG_CACHE = os.path.join(ROOT, "assets", "languages.json")


def public_languages():
    """Bytes per language across my public repos; falls back to the last good result."""
    langs = collections.Counter()
    try:
        for repo in rest(f"users/{USER}/repos", per_page=100, type="owner"):
            if repo["fork"] or repo["private"] or                     repo["full_name"].lower() in {r.lower() for r in CFG.get("exclude_repos", [])}:
                continue
            for lang, size in rest(f"repos/{repo['full_name']}/languages").items():
                langs["HTML / CSS" if lang in ("HTML", "CSS", "SCSS") else lang] += size
    except urllib.error.HTTPError as err:
        if os.path.exists(LANG_CACHE):
            print(f"languages: {err}, using the cached numbers")
            return collections.Counter(json.load(open(LANG_CACHE, encoding="utf-8")))
        raise
    with open(LANG_CACHE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(langs.most_common()), f, indent=2)
    return langs


def build_stack():
    langs = public_languages()
    top = langs.most_common(5)
    other = sum(langs.values()) - sum(v for _, v in top)
    if other:
        top.append(("Other", other))
    tot = sum(v for _, v in top)

    s = [tile(0, 0, 840, 132), t(24, 32, "Languages", DIM, 12, 600),
         t(816, 32, "share of code in my public repos", FAINT, 12, anchor="end"),
         '<clipPath id="bar"><rect x="24" y="48" width="792" height="8" rx="4"/></clipPath><g clip-path="url(#bar)">']
    x = 24
    for name, v in top:
        w = 792 * v / tot
        s.append(f'<rect x="{x:.1f}" y="48" width="{w + .5:.1f}" height="8" fill="{LANG_COLORS.get(name, FAINT)}"/>')
        x += w
    s.append("</g>")
    for i, (name, v) in enumerate(top):
        lx, ly = 24 + (i % 3) * 264, 86 + (i // 3) * 26
        s += [f'<circle cx="{lx + 4}" cy="{ly - 4}" r="4" fill="{LANG_COLORS.get(name, FAINT)}"/>',
              t(lx + 14, ly, name, SUB, 13.5, 600), t(lx + 14 + len(name) * 7.4 + 8, ly, f"{100 * v / tot:.1f}%", DIM, 13)]
    GW, GH, GG = 266, 112, 21
    for i, (name, lines) in enumerate(CFG["stack"]):
        gx, gy = (i % 3) * (GW + GG), 148 + (i // 3) * (GH + 16)
        s += [tile(gx, gy, GW, GH), t(gx + 20, gy + 30, name, DIM, 12, 600)]
        s += [t(gx + 20, gy + 58 + j * 22, line, SUB, 14) for j, line in enumerate(lines)]
    rows = (len(CFG["stack"]) + 2) // 3
    write_svg("stack.svg", 840, 148 + rows * (GH + 16) - 16, "Languages: " + ", ".join(n for n, _ in top) + ". " +
              " ".join(f"{n}: {', '.join(l)}." for n, l in CFG["stack"]), s)


def build_skills():
    skills = CFG.get("skills", [])
    if not skills:
        return False
    TW, TH, GX, GY = 410, 96, 20, 14
    rows = (len(skills) + 1) // 2
    s = []
    for i, (name, what, where) in enumerate(skills):
        x, y = (i % 2) * (TW + GX), (i // 2) * (TH + GY)
        s += [tile(x, y, TW, TH), t(x + 20, y + 30, name, TXT, 15, 600), t(x + 20, y + 54, what, SUB, 13)]
        if where:
            s.append(t(x + 20, y + 76, where, FAINT, 12))
    y = rows * (TH + GY)
    if CFG.get("spoken"):
        s += [tile(0, y, 840, 52), t(20, y + 31, "Spoken languages", DIM, 12, 600),
              t(160, y + 31, CFG["spoken"], SUB, 13.5)]
        y += 52 + GY
    write_svg("skills.svg", 840, y - GY, "Skills: " + "; ".join(f"{n} — {w} ({e})" for n, w, e in skills)
              + (f". Spoken languages: {CFG['spoken']}." if CFG.get("spoken") else ""), s)
    return True


def build_readme(public_repos):
    links = " &nbsp;·&nbsp;\n    ".join(f'<a href="https://github.com/{r}">{esc(title_of(r))}</a>' for r in public_repos)
    readme = f"""<!-- Generated by scripts/build_profile.py. Edit profile.json instead. -->
{themed_img('hero', esc(CFG['name']) + ' — ' + esc(CFG['subtitle']))}

<p align="center">
  <a href="mailto:{CFG['email']}">Email</a> &nbsp;·&nbsp;
  <a href="https://github.com/{USER}?tab=repositories">Repositories</a>
</p>

<br/>

<h3>This month</h3>

{themed_img('activity', 'My commits this month, day by day')}

<br/>

<h3>History</h3>

{themed_img('history', 'Project history')}

<p align="center">
  <sub>
    {links}
  </sub>
</p>

<br/>

<h3>Skills</h3>

{themed_img('skills', 'What I can do and where I used it')}

<br/>

<h3>Stack</h3>

{themed_img('stack', 'Languages and tools')}
"""
    with open(os.path.join(ROOT, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(readme)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", nargs="*", default=[], help="local git repos to read as well")
    ap.add_argument("--events", nargs="*", default=[], help="files with owner/repo|date|count lines")
    args = ap.parse_args()
    os.makedirs(os.path.join(ROOT, "assets"), exist_ok=True)
    today, events, private = collect(args.local, args.events)
    total, first = build_hero(today, events)
    build_month(today, events)
    public_repos = build_history(events, private, today)
    build_stack()
    build_skills()
    build_readme(public_repos)
    print(f"{total} commits since {first}, {len({e[1] for e in events})} repos, "
          f"source={'token' if OWNER_TOKEN else 'search'}{'+local' if args.local else ''}")


if __name__ == "__main__":
    main()
