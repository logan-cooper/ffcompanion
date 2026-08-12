#!/usr/bin/env bash
# Weekly refresh: new stats, then current league rosters.
#
# nflverse publishes on Tuesday once Monday Night Football is final, so running
# this any earlier gets you last week's numbers with this week's confidence —
# which is worse than not running it at all.

set -euo pipefail

# An NFL season is named for the year it STARTS, and it opens in September. So
# before September the current season is last calendar year — using date +%Y
# year-round asks nflverse for a season that does not exist yet and gets a 404
# traceback, which is the first thing a leaguemate would hit running this in the
# offseason.
default_season() {
  local year month
  year=$(date +%Y)
  month=$(date +%-m)
  if [ "$month" -lt 9 ]; then echo $((year - 1)); else echo "$year"; fi
}

SEASON="${SEASON:-$(default_season)}"

bold=$(tput bold 2>/dev/null || true)
green=$(tput setaf 2 2>/dev/null || true)
yellow=$(tput setaf 3 2>/dev/null || true)
off=$(tput sgr0 2>/dev/null || true)

step() { printf '\n%s==>%s %s\n' "$bold" "$off" "$*"; }
warn() { printf '  %s!%s %s\n' "$yellow" "$off" "$*"; }

cd "$(dirname "$0")/.."

# Tuesday is 2. Warn rather than refuse: an in-season Wednesday catch-up is a
# perfectly good reason to run this.
if [ "$(date +%u)" -lt 2 ]; then
  warn "It is $(date +%A). nflverse finalises the week on Tuesday —"
  warn "you may be pulling numbers that are still missing Monday night."
fi

step "Stats for $SEASON"
# nflverse publishes a season's file once that season starts. Asking early gets
# a 404, and a stack trace is a poor way to say "the season hasn't begun".
if ! make ingest SEASON="$SEASON" 2>&1 | tail -20; then
  printf '\n'
  warn "No published stats for $SEASON yet."
  warn "nflverse posts a season's file once games begin (early September)."
  warn "Your existing warehouse is untouched — the app works off it year-round."
  warn "To refresh a specific season instead:  SEASON=2025 make refresh"
  exit 0
fi

step "League rosters"
# Rosters change constantly in-season; stale ones give advice about players you
# no longer have. team_intent lives in its own table and survives a re-link.
#
# The username comes from .env, written there by setup.sh. It is not derivable
# from the database: league_users lists every manager in the league and nothing
# records which one is you.
username="${SLEEPER_USERNAME:-}"
if [ -z "$username" ] && [ -f .env ]; then
  username=$(awk -F= '/^SLEEPER_USERNAME=/ {print $2; exit}' .env | tr -d '"' | tr -d "'")
fi

if [ -n "$username" ]; then
  make link-league USERNAME="$username" SEASON="$SEASON"
else
  warn "No username on file. Either:"
  warn "  echo 'SLEEPER_USERNAME=<you>' >> .env"
  warn "  or run: make link-league USERNAME=<you> SEASON=$SEASON"
fi

step "Where things stand"
make status

printf '\n%sRefreshed.%s Cost: $0.00\n\n' "$green$bold" "$off"
