#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_MODELS="$ROOT/.local/models"
export OLLAMA_NO_CLOUD=1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1
exec "$ROOT/.local/ollama/ollama" serve
