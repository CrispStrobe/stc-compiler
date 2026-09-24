#!/usr/bin/env bash
# Deploy stc-compiler to Vercel — ON RELEASE, BY HAND, never on push.
#
# The Vercel git integration is DISCONNECTED on purpose (2026-08-25): the
# free tier caps deployments at 100/day account-wide, and auto-deploying
# every push burned through it mid-lane (the stm32f030 target sat built
# on main for hours while production served the old target list). Same
# policy as kerotakis: pushes are free, deploys are deliberate.
#
# Usage: scripts/deploy-vercel.sh [--prod]
#   default is a preview deploy; --prod promotes to stc-compiler.vercel.app.
#
# After a --prod deploy, run test-api.py (it tests PRODUCTION).
set -euo pipefail

cd "$(dirname "$0")/.."
SCOPE="crispstrobes-projects"
PROD=""
[ "${1:-}" = "--prod" ] && PROD="--prod"

# Refuse to deploy a dirty or unpushed tree: production must equal a
# commit that exists on origin/main, or the deploy is untraceable.
if [ -n "$(git status --porcelain)" ]; then
    echo "refusing: working tree is dirty — commit (and push) first"; exit 1
fi
if ! git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
    echo "refusing: HEAD is not on origin/main — push first"; exit 1
fi

# The deploy must go to THE stc-compiler project. An unlinked directory (a
# fresh clone, a git worktree -- .vercel/ is gitignored) does not fail: the
# CLI silently CREATES A NEW PROJECT named after the directory and deploys
# there. That happened on 2026-09-24 (a project "stc-arduino-cpp"). Refuse.
LINKED="$(sed -n 's/.*"projectName":"\([^"]*\)".*/\1/p' .vercel/project.json 2>/dev/null || true)"
if [ "$LINKED" != "stc-compiler" ]; then
    echo "refusing: this directory is linked to '${LINKED:-nothing}', not stc-compiler."
    echo "  copy .vercel/project.json from a linked checkout, or: vercel link --project stc-compiler"
    exit 1
fi

# Auth: a token from ~/.env first, else the logged-in CLI state. The token is
# tried FIRST because `vercel whoami` hangs forever with no TTY (a background
# shell, CI, an agent), so probing it first blocked every headless deploy.
TOKEN_ARGS=()
VERCEL_TOKEN="$(grep '^VERCEL_TOKEN=' ~/.env 2>/dev/null | cut -d= -f2- || true)"
if [ -n "${VERCEL_TOKEN:-}" ]; then
    TOKEN_ARGS=(--token "$VERCEL_TOKEN")
else
    # Bounded where `timeout` exists (Linux; macOS has it as gtimeout or not
    # at all), and never reading a terminal.
    TMO=""; command -v timeout >/dev/null && TMO="timeout 20"
    command -v gtimeout >/dev/null && TMO="gtimeout 20"
    if ! TERM=dumb $TMO vercel whoami </dev/null >/dev/null 2>&1; then
        echo "not logged in (or whoami timed out) and no VERCEL_TOKEN in ~/.env"; exit 1
    fi
fi

# Headless hardening (kerotakis lesson): the CLI crashes on uv_tty_init
# from a background shell — starve it of a TTY and take ITS exit code,
# then also refuse the exit-0-but-"Error:" shape (the rate-limit cap).
# macOS mktemp treats a suffix after the Xs as LITERAL - the first run
# creates exactly stc-vercel-deploy.XXXXXX.log and the second collides.
LOG="$(mktemp /tmp/stc-vercel-deploy.XXXXXX)"
set +e
TERM=dumb CI=1 vercel deploy ${TOKEN_ARGS[@]+"${TOKEN_ARGS[@]}"} --scope "$SCOPE" --yes $PROD \
    </dev/null >"$LOG" 2>&1
rc=$?
set -e
tail -5 "$LOG"
if [ "$rc" -ne 0 ] || grep -q '^Error:' "$LOG"; then
    echo "deploy FAILED (exit $rc; log: $LOG)"
    exit 1
fi
echo "deploy OK (log: $LOG)"
grep -Eo 'https://[a-z0-9.-]*vercel\.app[^ ]*' "$LOG" | tail -1
