#!/usr/bin/env bash
# DiffusionGemma 26B-A4B on Jetson Thor (sm_110, unified memory)
# Usage: ./run.sh              -> interactive chat
#        ./run.sh --visual     -> live canvas denoising view
#        ./run.sh -p "prompt"  -> one-shot
set -euo pipefail
DG=~/diffusiongemma
BIN=$DG/llama.cpp/build/bin/llama-diffusion-cli
MODEL=$DG/models/diffusiongemma-26B-A4B-it-GGUF/diffusiongemma-26B-A4B-it-Q8_0.gguf

EXTRA=()
if [[ "${1:-}" == "--visual" ]]; then EXTRA+=(--diffusion-visual); shift; fi

exec "$BIN" -m "$MODEL" \
  -ngl 99 \
  -cnv \
  -n 2048 \
  "${EXTRA[@]}" "$@"
