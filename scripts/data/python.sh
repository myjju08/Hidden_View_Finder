#!/usr/bin/env bash
# Local runtime only: never replaces system GDAL or the shared Python environment.
set -euo pipefail
# Native failures must not create an unbounded crash dump outside the budget.
ulimit -c 0
HVF_REPO=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
HVF_DEPS="$HVF_REPO/data/citywide/dependencies"
export PYTHONPATH="$HVF_DEPS/site:$HVF_DEPS/native/usr/lib/python3/dist-packages:$HVF_REPO/src:$HVF_REPO${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$HVF_DEPS/native/usr/lib/x86_64-linux-gnu:$HVF_DEPS/native/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GDAL_DATA="$HVF_DEPS/native/usr/share/gdal"
export PROJ_DATA="$HVF_DEPS/native/usr/share/proj"
export GDAL_DRIVER_PATH=disable
export GDAL_CACHEMAX=64
export GDAL_NUM_THREADS=1
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$HVF_REPO/data/citywide/staging/tmp"
export XDG_CACHE_HOME="$HVF_REPO/data/citywide/staging/cache"
export PIP_CACHE_DIR="$XDG_CACHE_HOME/pip"
export PROJ_NETWORK=OFF
exec python "$@"
