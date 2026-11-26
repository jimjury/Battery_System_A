#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_root"
runtime_python=${AB_PYTHON:-python3}
exec "$runtime_python" -m server_b.portable --open "$@"
