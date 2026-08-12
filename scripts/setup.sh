#!/usr/bin/env bash
# First-run setup. Safe to re-run: every step checks before it acts.
#
# The one genuinely rough edge here is a ~5GB model download, so this script
# says so and asks BEFORE starting rather than surprising you ten minutes in.

set -euo pipefail

MODEL="${MODEL:-qwen3:8b}"
SEASON="${SEASON:-2025}"
RAM_FLOOR_GB=16

bold=$(tput bold 2>/dev/null || true)
dim=$(tput dim 2>/dev/null || true)
red=$(tput setaf 1 2>/dev/null || true)
green=$(tput setaf 2 2>/dev/null || true)
yellow=$(tput setaf 3 2>/dev/null || true)
off=$(tput sgr0 2>/dev/null || true)

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$bold" "$off" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$green" "$off" "$*"; }
warn() { printf '  %s!%s %s\n' "$yellow" "$off" "$*"; }
die()  { printf '\n%sError:%s %s\n' "$red" "$off" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- what you get

total_ram_gb() {
  case "$(uname -s)" in
    Darwin) echo $(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 )) ;;
    Linux)  echo $(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 )) ;;
    *)      echo 0 ;;
  esac
}

cat <<BANNER

${bold}ffcompanion setup${off}

A fantasy football advisor that runs entirely on this machine.
No API key, no account, no per-question cost — now or ever.

${bold}What this will do${off}
  1. Check for Ollama (the local model runtime) and install it if missing
  2. Download the ${MODEL} model            ${dim}~5.2 GB, one time${off}
  3. Install Python dependencies            ${dim}~280 MB${off}
  4. Build the stats warehouse, 2023-2025   ${dim}~30 MB, a few minutes${off}
  5. Link your Sleeper league

${bold}Total download: about 5.5 GB${off}, nearly all of it the model.

BANNER

ram=$(total_ram_gb)
if [ "$ram" -gt 0 ] && [ "$ram" -lt "$RAM_FLOOR_GB" ]; then
  warn "This machine reports ${ram}GB of RAM."
  say  "    An 8B model needs about 6.5GB resident. Below ${RAM_FLOOR_GB}GB it will"
  say  "    swap and answers may take minutes instead of seconds. You can"
  say  "    continue, but a smaller model (MODEL=llama3.2:3b) will behave better."
  say
elif [ "$ram" -gt 0 ]; then
  ok "${ram}GB RAM — comfortable for an 8B model"
fi

if [ -t 0 ] && [ "${ASSUME_YES:-}" != "1" ]; then
  printf 'Continue? [y/N] '
  read -r reply
  case "$reply" in [yY]*) ;; *) say "Nothing downloaded. Re-run when ready."; exit 0 ;; esac
fi

# ------------------------------------------------------------------- toolchain

step "Checking the toolchain"

command -v git >/dev/null 2>&1 || die "git is required. Install it and re-run."

if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found (it manages Python and dependencies)"
  say  "    Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  say  "    Then re-run this script."
  die "uv is required."
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

# ---------------------------------------------------------------------- ollama

step "Checking Ollama"

if command -v ollama >/dev/null 2>&1; then
  ok "ollama installed"
else
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        say "  Installing via Homebrew..."
        brew install ollama
      else
        die "Install Ollama from https://ollama.com/download, then re-run."
      fi
      ;;
    Linux)
      say "  Installing via the official script..."
      curl -fsSL https://ollama.com/install.sh | sh
      ;;
    *)
      die "Install Ollama from https://ollama.com/download, then re-run."
      ;;
  esac
fi

if ! curl -sf --max-time 3 http://localhost:11434/api/version >/dev/null 2>&1; then
  say "  Starting the Ollama server..."
  # Backgrounded rather than a service, so this script never leaves something
  # running that the user did not ask for.
  ollama serve >/dev/null 2>&1 &
  for _ in $(seq 1 30); do
    curl -sf --max-time 2 http://localhost:11434/api/version >/dev/null 2>&1 && break
    sleep 1
  done
fi

curl -sf --max-time 3 http://localhost:11434/api/version >/dev/null 2>&1 \
  || die "Ollama will not start. Try 'ollama serve' in another terminal."
ok "ollama server responding on :11434"

# ----------------------------------------------------------------------- model

step "Model: $MODEL"

if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
  ok "already downloaded"
else
  say "  Downloading ${MODEL} (~5.2 GB). This is the slow part."
  ollama pull "$MODEL" || die "Could not pull $MODEL. Check your connection and re-run."
  ok "downloaded"
fi

# ------------------------------------------------------------------ python env

step "Python dependencies"
uv sync --quiet
ok "virtualenv ready"

if [ ! -f .env ]; then
  cp .env.example .env
  ok "created .env from .env.example"
else
  ok ".env already present, left alone"
fi

# -------------------------------------------------------------------- warehouse
#
# These checks query the database rather than grepping `status` output. Parsing
# a human-readable table is the same mistake as asserting on a label: the first
# version of this script looked for the word "league" in text that never
# contains it, so setup was not idempotent and would re-prompt every run.

count() { uv run --quiet python -c "
from advisor.db import query
print(query('SELECT COUNT(*) AS n FROM $1')[0]['n'])
" 2>/dev/null || echo 0; }

step "Stats warehouse (2023-2025)"

if [ "$(count player_week_stats)" -gt 0 ]; then
  ok "already built — 'make warehouse' rebuilds it"
else
  say "  Downloading and loading nflverse data. A few minutes."
  make warehouse
  ok "warehouse built"
fi

# ----------------------------------------------------------------------- league

step "Your Sleeper league"

remember_username() {
  # Store it so `make refresh` can re-pull rosters without asking again. Kept in
  # .env rather than inferred from the database: league_users lists every
  # manager in the league and marks none of them as you.
  #
  # The test is for a non-empty VALUE, not for the key. .env.example ships
  # `SLEEPER_USERNAME=`, so an earlier version that grepped for the key alone
  # always matched and silently never recorded anything — which only showed up
  # on a real fresh-clone run.
  if grep -q '^SLEEPER_USERNAME=..*' .env 2>/dev/null; then
    return 0
  fi
  # Drop the empty placeholder first; two definitions of one key is ambiguous.
  if [ -f .env ]; then
    grep -v '^SLEEPER_USERNAME=' .env > .env.tmp && mv .env.tmp .env
  fi
  printf 'SLEEPER_USERNAME=%s\n' "$1" >> .env
  ok "recorded SLEEPER_USERNAME in .env"
}

link_league() {
  make link-league USERNAME="$1" SEASON="$SEASON"
  remember_username "$1"
}

username="${SLEEPER_USERNAME:-}"

if [ "$(count leagues)" -gt 0 ]; then
  ok "a league is already linked — 'make link-league' re-links"
  # Linked but unrecorded is a real state: an install predating this script, or
  # one linked by hand. `make refresh` needs the name, so ask for it even though
  # there is nothing else left to do here.
  if ! grep -q '^SLEEPER_USERNAME=..*' .env 2>/dev/null; then
    if [ -z "$username" ] && [ -t 0 ]; then
      printf '  Sleeper username, for weekly refreshes (blank to skip): '
      read -r username
    fi
    [ -n "$username" ] && remember_username "$username"
  fi
elif [ -n "$username" ]; then
  link_league "$username"
elif [ -t 0 ]; then
  printf '  Sleeper username (blank to skip): '
  read -r username
  if [ -n "$username" ]; then
    link_league "$username"
  else
    warn "skipped — run: make link-league USERNAME=<you> SEASON=$SEASON"
  fi
else
  warn "no username given — run: make link-league USERNAME=<you> SEASON=$SEASON"
fi

# ------------------------------------------------------------------------- done

cat <<DONE

${green}${bold}Setup complete.${off}

  ${bold}make serve${off}    web UI at http://127.0.0.1:8000
  ${bold}make chat${off}     same thing in the terminal
  ${bold}make refresh${off}  update stats after Monday Night Football

Ollama must be running for either (${dim}ollama serve${off}).

${dim}Cost so far: \$0.00. Cost per question: \$0.00. There is no API key in this
project and nothing to bill.${off}

DONE
