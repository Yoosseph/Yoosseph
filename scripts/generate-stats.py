#!/usr/bin/env python3
"""Fetch GitHub statistics and render a self-contained neural-network SVG.

Python standard library only. Use GITHUB_TOKEN (or GH_TOKEN), or --use-gh locally.
All API queries finish successfully before the existing card is replaced.
"""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
METRICS = (
    ("stars", "Total Stars Earned", "star"),
    ("contributions", "Total Contributions", "activity"),
    ("commits", "Total Commits", "commit"),
    ("issues", "Total Issues", "issue"),
    ("prs", "PRs Created", "pr"),
    ("repositories", "Contributed to (all time)", "repo"),
)
PROFILE_QUERY = """
query($login: String!) {
  user(login: $login) {
    login
    contributionsCollection { contributionYears }
    issues { totalCount }
    pullRequests { totalCount }
  }
}
"""
STARS_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    repositories(first: 100, after: $after, ownerAffiliations: [OWNER]) {
      nodes { stargazerCount }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
COLLECTION_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar { totalContributions }
      totalRepositoriesWithContributedCommits
      commitContributionsByRepository(maxRepositories: 100) { repository { id } }
      issueContributions(first: 100) {
        nodes { issue { repository { id } } }
        pageInfo { hasNextPage endCursor }
      }
      pullRequestContributions(first: 100) {
        nodes { pullRequest { repository { id } } }
        pageInfo { hasNextPage endCursor }
      }
      repositoryContributions(first: 100) {
        nodes { repository { id } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


class StatsError(RuntimeError):
    """A failed or incomplete GitHub response; never publish partial totals."""


class GitHubAPI:
    def __init__(self, use_gh=False):
        self.use_gh = use_gh
        self.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not use_gh and not self.token:
            raise StatsError("Set GITHUB_TOKEN / GH_TOKEN, or use --use-gh after gh auth login.")

    def request(self, path, payload=None):
        method = "POST" if payload is not None else "GET"
        if self.use_gh:
            command = ["gh", "api", path, "--method", method]
            if payload is not None:
                command += ["--input", "-"]
            try:
                result = subprocess.run(
                    command, input=json.dumps(payload) if payload else None,
                    capture_output=True, text=True, timeout=90, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise StatsError("GitHub CLI is unavailable or timed out.") from exc
            if result.returncode:
                raise StatsError("GitHub CLI API request failed. Check authentication and permissions.")
            return json.loads(result.stdout)

        data = json.dumps(payload).encode() if payload is not None else None
        request = Request("https://api.github.com/" + path.lstrip("/"), data=data, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "neural-github-stats",
        }, method=method)
        for attempt in range(3):
            try:
                with urlopen(request, timeout=60) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise StatsError(f"GitHub API returned HTTP {exc.code}; previous SVG preserved.") from exc
            except (URLError, TimeoutError) as exc:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise StatsError("GitHub API is unreachable; previous SVG preserved.") from exc
        raise StatsError("GitHub API request failed.")

    def user(self, query, variables):
        result = self.request("graphql", {"query": query, "variables": variables})
        if result.get("errors") or not (result.get("data") or {}).get("user"):
            raise StatsError("GitHub GraphQL returned errors or no user; previous SVG preserved.")
        return result["data"]["user"]


def iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def pages(first, load_next):
    """Paginate completely and reject a missing/repeated cursor."""
    page = first
    cursors = set()
    while True:
        yield page
        info = page["pageInfo"]
        if not info["hasNextPage"]:
            return
        cursor = info["endCursor"]
        if not cursor or cursor in cursors:
            raise StatsError("GitHub returned an invalid pagination cursor.")
        cursors.add(cursor)
        page = load_next(cursor)


def connection_repositories(api, login, start, end, field, first):
    if field not in ("issueContributions", "pullRequestContributions", "repositoryContributions"):
        raise StatsError("Unsupported contribution connection.")
    resource = {"issueContributions": "issue", "pullRequestContributions": "pullRequest",
                "repositoryContributions": None}[field]
    selection = "repository { id }"
    if resource:
        selection = resource + " { " + selection + " }"
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!, $after: String) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          FIELD(first: 100, after: $after) {
            nodes { SELECTION }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """.replace("FIELD", field).replace("SELECTION", selection)
    variables = {"login": login, "from": iso(start), "to": iso(end)}

    def next_page(cursor):
        return api.user(query, {**variables, "after": cursor})["contributionsCollection"][field]

    repository_ids = set()
    for page in pages(first, next_page):
        for node in page["nodes"]:
            # Restricted/deleted contributions may not expose a repository.
            item = node.get(resource) if resource and node else node
            if item and item.get("repository"):
                repository_ids.add(item["repository"]["id"])
    return repository_ids


def collect_interval(api, login, start, end):
    collection = api.user(COLLECTION_QUERY, {
        "login": login, "from": iso(start), "to": iso(end),
    })["contributionsCollection"]
    commits = collection["commitContributionsByRepository"]
    if collection["totalRepositoriesWithContributedCommits"] > len(commits):
        # GitHub caps the commit-repository list at 100. Split busy windows
        # instead of silently undercounting repositories; avoid boundary overlap.
        if (end - start).total_seconds() < 2:
            raise StatsError("GitHub's commit-repository cap cannot be resolved for this interval.")
        midpoint = start + timedelta(seconds=int((end - start).total_seconds() // 2))
        _, left_repos = collect_interval(api, login, start, midpoint)
        _, right_repos = collect_interval(api, login, midpoint + timedelta(seconds=1), end)
        # Calendars operate on days: retain the original total to avoid double counting.
        return collection["contributionCalendar"]["totalContributions"], left_repos | right_repos

    repositories = {item["repository"]["id"] for item in commits if item.get("repository")}
    for field in ("issueContributions", "pullRequestContributions", "repositoryContributions"):
        repositories.update(connection_repositories(api, login, start, end, field, collection[field]))
    return collection["contributionCalendar"]["totalContributions"], repositories


def fetch_stats(api, login, now=None):
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    profile = api.user(PROFILE_QUERY, {"login": login})
    login = profile["login"]
    stats = {"stars": 0, "contributions": 0, "commits": 0,
             "issues": profile["issues"]["totalCount"],
             "prs": profile["pullRequests"]["totalCount"], "repositories": 0}

    def star_page(cursor):
        return api.user(STARS_QUERY, {"login": login, "after": cursor})["repositories"]

    for page in pages(star_page(None), star_page):
        stats["stars"] += sum(repo["stargazerCount"] for repo in page["nodes"])

    # Match the old card's include_all_commits=true REST search semantics.
    search_path = "search/commits?" + urlencode({"q": f"author:{login}", "per_page": 1})
    for attempt in range(3):
        search = api.request(search_path)
        if not search.get("incomplete_results", True):
            stats["commits"] = search["total_count"]
            break
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    else:
        raise StatsError("GitHub commit search was incomplete; previous SVG preserved.")

    repositories = set()
    # Imported commits can predate signup, so use contributionYears, not createdAt.
    years = sorted(set(profile["contributionsCollection"]["contributionYears"]) | {now.year})
    for year in years:
        if year > now.year:
            continue
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = min(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc), now)
        count, annual_repos = collect_interval(api, login, start, end)
        stats["contributions"] += count
        repositories.update(annual_repos)
    stats["repositories"] = len(repositories)
    validate_stats(stats)
    return login, stats, now.date().isoformat()


def validate_stats(stats):
    for key, _, _ in METRICS:
        if type(stats.get(key)) is not int or stats[key] < 0:
            raise StatsError(f"Invalid nonnegative integer statistic: {key}")


def format_number(value):
    if value < 1_000_000:
        return f"{value:,}"
    for power, suffix in ((12, "T"), (9, "B"), (6, "M")):
        if value >= 10 ** power:
            number = Decimal(value) / (10 ** power)
            if number < 1000:
                return f"{number:.1f}".rstrip("0").rstrip(".") + suffix
            return f"{Decimal(value):.1E}"
    raise StatsError("Could not format statistic.")


ICONS = {
    "star": '<path d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.9L12 17.8l-6.2 3.3L7 14.2 2 9.3l6.9-1Z"/>',
    "activity": '<path d="M2 12h4l3-7 5 14 3-7h5"/>',
    "commit": '<path d="M2 12h6m8 0h6"/><circle cx="12" cy="12" r="4"/>',
    "issue": '<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4v.1"/>',
    "pr": '<circle cx="6" cy="4" r="2"/><circle cx="6" cy="20" r="2"/><circle cx="18" cy="20" r="2"/><path d="M6 6v12m12 0v-7a5 5 0 0 0-5-5h-1m3-3-3 3 3 3"/>',
    "repo": '<path d="M5 3h14v18H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm0 0v14m0 0h14M8 7h7"/>',
}


def icon(kind, x, y, size=18, css=""):
    return (f'<g class="{css}" transform="translate({x} {y}) scale({size / 24})" '
            'fill="none" stroke="currentColor" stroke-width="1.65" '
            f'stroke-linecap="round" stroke-linejoin="round">{ICONS[kind]}</g>')


def render_svg(login, stats, updated):
    validate_stats(stats)
    rows = []
    for index, (key, label, kind) in enumerate(METRICS):
        rows.append(f'''<g transform="translate(0 {index * 46})">
      <rect class="stat-panel" width="368" height="39" rx="8" fill="url(#glass)" stroke="#263547" stroke-opacity=".55"/>
      {icon(kind, 13, 10, css="metric-icon")}
      <text class="metric-label" x="43" y="25">{label}</text>
      <text class="metric-value" x="353" y="26"><title>{stats[key]:,}</title>{format_number(stats[key])}</text>
    </g>''')
    satellites = (
        ("star", "Stars", 535, 113, "M535 113C602 110 577 188 650 225", 0),
        ("activity", "Activity", 669, 83, "M669 83C716 131 611 173 650 225", 2),
        ("commit", "Commits", 789, 151, "M789 151C730 146 738 203 650 225", 4),
        ("pr", "Pull requests", 784, 298, "M784 298C718 312 741 226 650 225", 1),
        ("issue", "Issues", 649, 354, "M649 354C599 309 696 280 650 225", 5),
        ("repo", "Repositories", 509, 281, "M509 281C568 301 570 230 650 225", 3),
    )
    connections, nodes = [], []
    for kind, label, x, y, curve, delay in satellites:
        connections.append(f'''<path d="{curve}" stroke="url(#synapse)" stroke-width="1.3"/>
      <path class="signal signal-{delay}" d="{curve}" stroke="#8ce6f2" stroke-width="2" pathLength="100" stroke-dasharray="2 98" stroke-dashoffset="100" opacity="0"/>''')
        nodes.append(f'''<g transform="translate({x} {y})">
      <circle r="31" fill="url(#node-halo)"/>
      <circle class="satellite satellite-{delay}" r="23" fill="#101e2d" stroke="#3a819f" stroke-opacity=".75"/>
      {icon(kind, -11, -11, 22)}
      <circle cx="17" cy="-17" r="2.3" fill="#80d8ee"/>
      <text class="node-label" y="43" text-anchor="middle">{label}</text>
    </g>''')
    particles = [(449, 78, 2), (476, 179, 2.5), (445, 343, 2), (553, 370, 2.5),
                 (729, 375, 2), (833, 350, 2.5), (829, 228, 2), (811, 64, 2),
                 (744, 86, 2), (590, 67, 1.8), (563, 207, 2), (708, 153, 1.6),
                 (703, 308, 2), (479, 391, 1.6), (588, 331, 1.6)]
    replacements = {
        "LOGIN": escape(login), "UPDATED": escape(updated),
        "SUMMARY": escape("; ".join(f"{label}: {stats[key]:,}" for key, label, _ in METRICS)),
        "ROWS": "\n".join(rows), "CONNECTIONS": "\n".join(connections), "NODES": "\n".join(nodes),
        "PARTICLES": "".join(f'<circle cx="{x}" cy="{y}" r="{r}"/>' for x, y, r in particles),
    }
    template = (ROOT / "scripts/neural-stats-template.svg").read_text(encoding="utf-8")
    return re.sub(r"\{\{([A-Z]+)\}\}", lambda match: replacements[match.group(1)], template)


def write_svg(output, svg):
    ET.fromstring(svg)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.read_text(encoding="utf-8") == svg:
        return False
    temporary = output.with_suffix(".svg.tmp")
    temporary.write_text(svg, encoding="utf-8", newline="\n")
    temporary.replace(output)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default=os.environ.get("PROFILE_USERNAME", "Yoosseph"))
    parser.add_argument("--output", type=Path, default=ROOT / "assets/neural-github-stats.svg")
    parser.add_argument("--use-gh", action="store_true", help="Use the authenticated GitHub CLI locally")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", args.username):
        parser.error("Invalid GitHub username")
    try:
        login, stats, updated = fetch_stats(GitHubAPI(args.use_gh), args.username)
        changed = write_svg(args.output, render_svg(login, stats, updated))
        print(f"{'Generated' if changed else 'Unchanged'}: {args.output}")
        print(json.dumps(stats, sort_keys=True))
    except (StatsError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Stats generation failed ({type(exc).__name__}); previous SVG preserved.", file=sys.stderr)
        if isinstance(exc, StatsError):
            print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
