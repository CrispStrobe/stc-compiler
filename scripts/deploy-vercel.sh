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

# Auth: the logged-in CLI state, with a token from ~/.env as fallback.
TOKEN_ARGS=()
if ! vercel whoami >/dev/null 2>&1; then
    VERCEL_TOKEN="$(grep '^VERCEL_TOKEN=' ~/.env 2>/dev/null | cut -d= -f2- || true)"
    [ -n "${VERCEL_TOKEN:-}" ] || { echo "not logged in and no VERCEL_TOKEN in ~/.env"; exit 1; }
    TOKEN_ARGS=(--token "$VERCEL_TOKEN")
fi

# Headless hardening (kerotakis lesson): the CLI crashes on uv_tty_init
# from a background shell — starve it of a TTY and take ITS exit code,
# then also refuse the exit-0-but-"Error:" shape (the rate-limit cap).
LOG="$(mktemp /tmp/stc-vercel-deploy.XXXXXX.log)"
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
