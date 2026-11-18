#!/bin/sh
set -eu
task_project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$task_project_dir"
exec python3 -m iwork_collector install-native "$@"
