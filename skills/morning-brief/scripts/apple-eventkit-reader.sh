#!/bin/zsh
set -euo pipefail
umask 077

script_dir=${0:A:h}
exec /usr/bin/python3 -I "$script_dir/apple-eventkit-reader/reader.py" "$@"
