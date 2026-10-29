#!/usr/bin/env python3
"""
oss_daily.py - build today's open-source to-do list from GitHub.

Does three things:
  0. DISCOVERS projects: searches GitHub for active, non-archived repos that
     match your topics (and optionally a language) and have good-first-issues
  1. Finds "good first issue" / "help wanted" issues in them that are open,
     unassigned, and have no linked PR yet
  2. Finds the most active open PRs (by comment count) updated recently

Then writes a markdown checklist you can work through today.

Usage:
    export GITHUB_TOKEN=ghp_xxx        # optional but strongly recommended
    python oss_daily.py                                   # auto-discover projects
    python oss_daily.py --topics fastapi rag --language python
    python oss_daily.py --min-stars 200 --max-stars 8000 --top-repos 8
    python oss_daily.py --repos dlt-hub/dlt PrefectHQ/prefect   # skip discovery
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

ISSUES_URL = "https://api.github.com/search/issues"
REPOS_URL = "https://api.github.com/search/repositories"

# Topics used to DISCOVER projects. Edit these to match what you want to learn.
DEFAULT_TOPICS = ["fastapi", "data-pipeline", "swiftui", "swift"]

BEGINNER_LABELS = ["good first issue", "good-first-issue", "help wanted"]


# ---------- Step 1: talk to GitHub ----------

def make_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print("! No GITHUB_TOKEN set: search is limited to ~10 requests/min.\n")
    return headers


def search(url, query, sort, per_page, headers, retry=True):
    params = {"q": query, "sort": sort, "order": "desc", "per_page": per_page}
    resp = requests.get(url, headers=headers, params=params, timeout=20)
    if resp.status_code in (403, 429):
        # Rate limited: wait until the window resets (max ~65s), then retry once.
        reset = int(resp.headers.get("x-ratelimit-reset", 0))
        wait = min(max(reset - int(time.time()), 1), 65)
        if retry:
            print(f"  (rate limited, waiting {wait}s...)")
            time.sleep(wait)
            return search(url, query, sort, per_page, headers, retry=False)
        print(f"! Still rate limited, skipping: {query}")
        return []
    resp.raise_for_status()
    return resp.json().get("items", [])


# ---------- Step 2: DISCOVER projects worth contributing to ----------

def discover_repos(topics, language, min_stars, max_stars, active_days, top, headers):
    """
    One search per topic. Each query keeps only repos that:
      - have more than 2 open good-first-issues   (good-first-issues:>2)
      - were pushed to recently                   (pushed:>=...)
      - are not archived and not forks
      - have a star count in a friendly range     (big enough to be real,
                                                   small enough that maintainers reply)
    """
    since = (date.today() - timedelta(days=active_days)).isoformat()
    groups = []
    for topic in topics:
        query = (
            f"topic:{topic} good-first-issues:>2 stars:{min_stars}..{max_stars} "
            f"pushed:>={since} archived:false fork:false"
        )
        if language:
            query += f" language:{language}"
        print(f"Discovering projects for topic '{topic}' ...")
        found = search(REPOS_URL, query, sort="updated", per_page=10, headers=headers)
        groups.append(found)

    # Round-robin across topics, skipping duplicates
    seen, picked = set(), []
    while len(picked) < top and any(groups):
        for group in groups:
            while group:
                repo = group.pop(0)
                if repo["full_name"] not in seen:
                    seen.add(repo["full_name"])
                    picked.append(repo)
                    break
            if len(picked) >= top:
                break
    return picked


# ---------- Step 3: find issues and PRs per repo ----------

def find_issues(repo, headers, fetch=10):
    labels = ",".join(f'"{label}"' for label in BEGINNER_LABELS)
    query = f"repo:{repo} is:issue is:open no:assignee -linked:pr label:{labels}"
    items = search(ISSUES_URL, query, sort="updated", per_page=fetch, headers=headers)
    # Prefer quieter issues (fewer comments = less likely already being worked on)
    items.sort(key=lambda i: i["comments"])
    return items


def find_active_prs(repo, headers, days, fetch=10):
    since = (date.today() - timedelta(days=days)).isoformat()
    query = f"repo:{repo} is:pr is:open draft:false updated:>={since}"
    return search(ISSUES_URL, query, sort="comments", per_page=fetch, headers=headers)


# ---------- Step 4: pick a balanced set ----------

def interleave(groups, n):
    """Take items round-robin so one busy repo doesn't fill the whole list."""
    picked = []
    while len(picked) < n and any(groups):
        for group in groups:
            if group and len(picked) < n:
                picked.append(group.pop(0))
    return picked


def repo_of(item):
    return item["repository_url"].split("/repos/")[-1]


def age_in_days(iso_ts):
    updated = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - updated).days


# ---------- Step 5: render the to-do list ----------

def render(issues, prs, projects):
    today = date.today().strftime("%A, %d %b %Y")
    lines = [f"# Open-source to-do: {today}", ""]

    if projects:
        lines += ["## Projects found", ""]
        for p in projects:
            desc = (p.get("description") or "no description")[:90]
            lines.append(
                f"- **{p['full_name']}** ({p['stargazers_count']} stars, "
                f"pushed {age_in_days(p['pushed_at'])}d ago): {desc}"
            )
        lines.append("")

    lines += ["## 1. Contribute today (good first issues)", ""]
    if not issues:
        lines += ["_No matching issues found. Try more repos or check again tomorrow._", ""]
    for i in issues:
        labels = ", ".join(l["name"] for l in i["labels"][:3])
        lines += [
            f"### [{repo_of(i)}] {i['title']}",
            f"{i['html_url']}  ",
            f"{i['comments']} comments | updated {age_in_days(i['updated_at'])}d ago | {labels}",
            "",
            "- [ ] Read CONTRIBUTING.md and the full issue thread",
            "- [ ] Comment to claim it (wait for a maintainer reply if the repo asks)",
            "- [ ] Fork, clone, and reproduce / locate the relevant code",
            "- [ ] Make the smallest fix, add or update a test",
            "- [ ] Open the PR and link the issue (`Fixes #123`)",
            "",
        ]

    lines += ["## 2. Learn from active PRs (review or read)", ""]
    if not prs:
        lines += ["_No active PRs found in this window._", ""]
    for p in prs:
        lines += [
            f"### [{repo_of(p)}] {p['title']}",
            f"{p['html_url']}  ",
            f"{p['comments']} comments | by @{p['user']['login']} | updated {age_in_days(p['updated_at'])}d ago",
            "",
            "- [ ] Read the description and skim the diff",
            "- [ ] Check out the branch locally and run the tests",
            "- [ ] Leave one useful comment (question, edge case, or typo)",
            "",
        ]

    lines += [
        "## 3. Daily routine",
        "",
        "- [ ] Reply to any comments on my open PRs",
        "- [ ] Rebase or update a stale PR if needed",
        "- [ ] Note what I learned today",
        "",
    ]
    return "\n".join(lines)


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description="Daily open-source to-do list")
    parser.add_argument("--repos", nargs="+", help="owner/name list; skips discovery")
    parser.add_argument("--topics", nargs="+", default=DEFAULT_TOPICS, help="topics to discover by")
    parser.add_argument("--language", help="e.g. python or swift (optional)")
    parser.add_argument("--min-stars", type=int, default=1000)
    parser.add_argument("--max-stars", type=int, default=15000)
    parser.add_argument("--active-days", type=int, default=100, help="repo must be pushed within N days")
    parser.add_argument("--top-repos", type=int, default=6, help="how many projects to scan")
    parser.add_argument("--issues", type=int, default=3, help="how many issues to pick")
    parser.add_argument("--prs", type=int, default=3, help="how many active PRs to pick")
    parser.add_argument("--days", type=int, default=7, help="PR activity window in days")
    parser.add_argument("--out", default=f"oss-todo-{date.today().isoformat()}.md")
    args = parser.parse_args()

    headers = make_headers()

    # Step A: decide which projects to scan
    if args.repos:
        projects, repo_names = [], args.repos
    else:
        projects = discover_repos(
            args.topics, args.language, args.min_stars, args.max_stars,
            args.active_days, args.top_repos, headers,
        )
        repo_names = [p["full_name"] for p in projects]
        if not repo_names:
            print("No projects matched. Loosen --min-stars, --active-days or --topics.")
            return 1

    # Step B: pull issues and active PRs from each project
    issue_groups, pr_groups = [], []
    for repo in repo_names:
        print(f"Scanning {repo} ...")
        try:
            issue_groups.append(find_issues(repo, headers))
            pr_groups.append(find_active_prs(repo, headers, args.days))
        except requests.RequestException as exc:
            print(f"! Skipping {repo}: {exc}")

    issues = interleave(issue_groups, args.issues)
    prs = interleave(pr_groups, args.prs)

    report = render(issues, prs, projects)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)

    print("\n" + report)
    print(f"Saved to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
