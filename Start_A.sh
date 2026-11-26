#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec "${AB_PYTHON:-python3}" -m desktop_a.main --open "$@"
