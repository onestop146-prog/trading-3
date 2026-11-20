#!/usr/bin/env bash
# Saves the bot's files (trades, performance, state, reports) back into the
# repo after each run, so the next run can read them. GitHub runners forget
# everything between runs, so the repo IS the bot's memory.
set -uo pipefail
msg="${1:-bot update}"
git config user.name "paper-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add data 2>/dev/null
git add backtest_results 2>/dev/null
if git diff --cached --quiet; then
  echo "Nothing new to save."
  exit 0
fi
git commit -q -m "$msg [skip ci]"
for attempt in 1 2 3 4; do
  if git pull -q --rebase origin "$GITHUB_REF_NAME" && git push -q origin "HEAD:$GITHUB_REF_NAME"; then
    echo "Saved."
    exit 0
  fi
  sleep $((attempt * 5))
done
echo "Could not push results to GitHub."
exit 1
