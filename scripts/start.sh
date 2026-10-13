#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f config.json ]; then
  printf 'Run ghostdev init and configure config.json first.\n'
  exit 1
fi
ghostdev worker &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true' EXIT INT TERM
ghostdev serve
