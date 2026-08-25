#!/usr/bin/env bash
# Deploy stc-compiler to the Hugging Face Space mirror (cstr/stc-compiler).
#
# The Space is the second host for the compile API — Docker SDK, same
# app.py and the same vendored toolchains, listening on 7860. It exists
# because Vercel's free tier caps deployments at 100/day account-wide
# (hit 2026-08-25) and a second host makes the compile service survive
# one platform's bad day. Like the Vercel deploy: ON RELEASE, BY HAND.
#
# Usage: scripts/deploy-hf.sh
# Auth: HF_TOKEN from /Users/christianstrobele/code/.env (account: cstr),
#       or an already-logged-in `hf` CLI.
set -euo pipefail

cd "$(dirname "$0")/.."
SPACE="cstr/stc-compiler"

if [ -n "$(git status --porcelain)" ]; then
    echo "refusing: working tree is dirty — commit (and push) first"; exit 1
fi
if ! git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
    echo "refusing: HEAD is not on origin/main — push first"; exit 1
fi

TOKEN_ARGS=()
if ! hf auth whoami >/dev/null 2>&1; then
    HF_TOKEN="$(grep '^HF_TOKEN=' "$HOME/code/.env" 2>/dev/null | cut -d= -f2- || true)"
    [ -n "${HF_TOKEN:-}" ] || { echo "not logged in and no HF_TOKEN in ~/code/.env"; exit 1; }
    TOKEN_ARGS=(--token "$HF_TOKEN")
fi

# Stage a clean copy: the Space wants ITS OWN README.md (YAML front
# matter selects the Docker SDK), never the repo's; everything else is
# the repo as pushed. Exclude the git dir, caches, and test corpora the
# service never reads at runtime.
STAGE="$(mktemp -d /tmp/stc-hf-stage.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
rsync -a \
    --exclude '.git' --exclude '__pycache__' --exclude '.vercel' \
    --exclude 'docs' --exclude 'test_*.py' --exclude 'scripts' \
    ./ "$STAGE/"

cat > "$STAGE/README.md" <<EOF
---
title: stc-compiler
emoji: 🔩
colorFrom: gray
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# stc-compiler

Compile service for BrickWright: C / BrickWright pseudocode / Keil C51 →
Intel HEX or raw binary, for 8051 (SDCC), AVR (avr-gcc), ARM Cortex-M
(arm-none-eabi-gcc: rp2040, stm32f030) and 6502 (cc65).

Mirror of <https://stc-compiler.vercel.app> — source:
<https://github.com/CrispStrobe/stc-compiler> @ $(git rev-parse --short HEAD).

POST /compile with {"code", "language", "target", "format"}; GET /docs
for the OpenAPI browser.
EOF

echo "uploading to $SPACE (this pushes ~130 MB of toolchains on first run)…"
hf upload "$SPACE" "$STAGE" . --repo-type space "${TOKEN_ARGS[@]}" \
    --commit-message "deploy $(git rev-parse --short HEAD)"

echo "deploy pushed — build status: https://huggingface.co/spaces/$SPACE"
echo "when Running, the API answers at: https://cstr-stc-compiler.hf.space/compile"
