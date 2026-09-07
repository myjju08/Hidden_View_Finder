#!/usr/bin/env bash
set -euo pipefail
# Native library setup is explicit: Ubuntu 24.04 supplies libgdal-dev 3.8.4.
#   apt-get install --no-install-recommends libgdal-dev
# This script installs into a project-local environment only.
if ! command -v gdal-config >/dev/null; then
  echo 'Missing gdal-config. Install libgdal-dev 3.8.4 from your OS package manager first.' >&2
  exit 1
fi
if [ "$(gdal-config --version)" != '3.8.4' ]; then
  echo 'requirements.lock requires native GDAL 3.8.4. Use a matching environment; do not mix GDAL libraries.' >&2
  exit 1
fi
python - <<'PY'
import shutil
from pathlib import Path
free=shutil.disk_usage('.').free
current=sum(p.stat().st_size for p in Path('.').rglob('*') if p.is_file())
allowance=2*1024**3
if free-allowance < 8*1024**3 or current+allowance > 20*1024**3:
    raise SystemExit('Installation preflight failed: reserve 2 GiB installation peak, 8 GiB free, within 20 GiB project budget.')
PY
python -m venv .venv
.venv/bin/python -m pip install --no-cache-dir 'numpy==2.5.2' 'setuptools>=68' wheel
.venv/bin/python -m pip install --no-cache-dir --no-build-isolation -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
