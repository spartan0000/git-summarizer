#!/usr/bin/env python3
"""
Pulls commits (with diffs) authored by you across all your GitHub repos
in the last 24 hours, and summarizes them using a local Ollama model.

Requirements:
    pip install requests --break-system-packages

Env vars:
    GITHUB_TOKEN     - a GitHub personal access token (classic or fine-grained,
                        needs 'repo' read scope for private repos)
    GITHUB_USER      - your GitHub username
    SLACK_WEBHOOK_URL - a Slack incoming webhook URL (optional; if unset,
                        summary just prints to stdout)

Usage:
    python3 git_summarizer.py

Cron example (runs daily at 8am):
    0 8 * * * /usr/bin/python3 /path/to/github_diff_summary.py >> /path/to/logfile.log 2>&1
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone
import ollama
from ollama import chat
from dotenv import load_dotenv

load_dotenv()



GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_USER = os.environ.get("GITHUB_USER")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
OLLAMA_MODEL = "gemma4:12b"  
LOOKBACK_HOURS = 24
MAX_DIFF_CHARS = 12000  # kept for reference; superseded by PER_COMMIT_MAX_CHARS below
PER_COMMIT_MAX_CHARS = 6000  # truncate any single commit's diff before summarizing it
SLACK_MAX_CHARS = 3500  # Slack messages cap around 4000 chars per block

if not GITHUB_TOKEN or not GITHUB_USER:
    sys.exit("Set GITHUB_TOKEN and GITHUB_USER environment variables first.")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)


def get_recently_pushed_repos():
    """Repos you own or collaborate on that were pushed to within the
    lookback window. Sorted by pushed date, so we can stop paging as soon
    as we hit repos older than the cutoff."""
    repos = []
    url = "https://api.github.com/user/repos"
    params = {"per_page": 100, "affiliation": "owner,collaborator", "sort": "pushed", "direction": "desc"}
    while url:
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        page = resp.json()
        stop = False
        for repo in page:
            pushed_at = repo.get("pushed_at")
            if not pushed_at:
                continue
            pushed_dt = datetime.strptime(pushed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if pushed_dt < since:
                stop = True
                break
            repos.append(repo)
        if stop:
            break
        url = resp.links.get("next", {}).get("url")
        params = None  # only needed on first request
    return repos


def get_recent_commits(owner, repo):
    """Commits by you in this repo within the lookback window."""
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    params = {
        "since": since.isoformat(),
        "author": GITHUB_USER,
        "per_page": 100,
    }
    resp = requests.get(url, headers=HEADERS, params=params)
    if resp.status_code != 200:
        return []
    return resp.json()


def get_commit_diff(owner, repo, sha):
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    files = data.get("files", [])
    patches = []
    for f in files:
        patch = f.get("patch")
        if patch:
            patches.append(f"--- {f['filename']} ---\n{patch}")
    return "\n".join(patches)


def summarize_diff(diff_label, diff_text):
    """First pass: summarize a single commit's diff."""
    prompt = (
        "Summarize this single git commit's diff in 2-4 bullet points. "
        "Be concrete about what changed. Flag anything risky or incomplete.\n\n"
        f"{diff_label}\n{diff_text}"
    )
    resp = chat(
        model = OLLAMA_MODEL,
        messages = [{'role': 'user', 'content': prompt}],
        stream = False
    )
    
    
    return resp.message.content


def summarize_with_ollama(text):
    """Second pass: summarize a list of per-commit summaries into one digest."""
    prompt = (
        "Below are summaries of individual commits from the last 24 hours. "
        "Produce one consolidated summary grouped by logical change/feature, "
        "not by commit. Call out anything that looks risky or incomplete. "
        "Keep it concise (bullet points).\n\n" + text
    )
    resp = chat(
            model = OLLAMA_MODEL,
            messages = [{'role': 'user', 'content': prompt}],
            stream = False
        )
    
    return resp.message.content


def send_to_slack(text):
    if not SLACK_WEBHOOK_URL:
        return
    # Slack blocks/webhooks reject overly long text, so chunk if needed
    chunks = [text[i:i + SLACK_MAX_CHARS] for i in range(0, len(text), SLACK_MAX_CHARS)] or [text]
    for chunk in chunks:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"text": chunk})
        if resp.status_code != 200:
            print(f"[warn: Slack post failed: {resp.status_code} {resp.text}]", file=sys.stderr)


def main():
    repos = get_recently_pushed_repos()
    all_diffs = []  # list of (label, diff_text)

    for repo in repos:
        owner = repo["owner"]["login"]
        name = repo["name"]
        commits = get_recent_commits(owner, name)
        for c in commits:
            sha = c["sha"]
            msg = c["commit"]["message"].splitlines()[0]
            diff = get_commit_diff(owner, name, sha)
            if diff:
                label = f"## {owner}/{name} - {sha[:7]} - {msg}"
                all_diffs.append((label, diff))

    header = (f"=== GitHub activity summary for {GITHUB_USER} "
              f"({datetime.now(timezone.utc).date()}) ===\n")

    if not all_diffs:
        msg = f"{header}\nNo commits found in the last {LOOKBACK_HOURS}h for {GITHUB_USER}."
        print(msg)
        send_to_slack(msg)
        return

    # Pass 1: summarize each commit individually, truncating any single
    # oversized diff so one huge commit can't blow the context window.
    per_commit_summaries = []
    for label, diff in all_diffs:
        if len(diff) > PER_COMMIT_MAX_CHARS:
            diff = diff[:PER_COMMIT_MAX_CHARS] + "\n[diff truncated]"
        print(f"Summarizing {label}...")
        try:
            
            summary = summarize_diff(label, diff)
        except Exception as e:
            print(f"[warn: failed to summarize {label}: {e}]", file=sys.stderr)
            summary = "[summary failed - see logs]"

        per_commit_summaries.append(f"{label}\n{summary}")

    # Pass 2: summarize the list of per-commit summaries into one digest.
    combined_summaries = "\n\n".join(per_commit_summaries)
    print(header)
    try:
        final_summary = summarize_with_ollama(combined_summaries)
    except Exception as e:
        print(f"[warn: failed to generate final summary: {e}]", file=sys.stderr)
        final_summary = "[final summary failed - see per-commit summaries above/logs]"
    print(final_summary)

    send_to_slack(f"{header}\n{final_summary}")


if __name__ == "__main__":
    main()