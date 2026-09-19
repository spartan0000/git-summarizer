# git-diff-summarizer

Pulls your GitHub commits (with diffs) from the last 24 hours across all repos you own or collaborate on, summarizes them with a local Ollama model, and optionally posts the digest to Slack.

## How it works

1. Finds repos pushed to in the lookback window (default 24h).
2. Fetches your commits in each repo and pulls the diff for each one.
3. Summarizes each commit individually (truncating oversized diffs), then combines those summaries into one digest grouped by logical change, with risky/incomplete work flagged.
4. Prints the digest to stdout and sends it to Slack if a webhook is configured.

## Setup

```bash
pip install -r requirements.txt
```

Requires a local [Ollama](https://ollama.com) instance running with the model set in `OLLAMA_MODEL` (see `git_summarizer.py`) pulled.

Create a `.env` file (already gitignored) with:

```
GITHUB_TOKEN=your_github_personal_access_token   # needs 'repo' read scope for private repos
GITHUB_USER=your_github_username
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...   # optional
```

## Usage

```bash
python git_summarizer.py
```

## Scheduling

Run daily via cron:

```
0 8 * * * /usr/bin/python3 /path/to/git_summarizer.py >> /path/to/logfile.log 2>&1
```
