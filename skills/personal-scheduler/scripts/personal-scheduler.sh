#!/bin/zsh
set -euo pipefail
umask 077

script_dir=${0:A:h}
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 "$script_dir/scheduler_executor.py" "$@"
