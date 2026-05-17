from __future__ import annotations

import os

CORE_UTIL_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(CORE_UTIL_DIR)
BASE_PROJECT_DIR = os.path.dirname(CORE_DIR)

BASE_DIR = os.path.join(BASE_PROJECT_DIR, "data")
INVENTORY_DIR = os.path.join(BASE_DIR, "inventory")
PROJECT_DIR = os.path.join(BASE_DIR, "project")
ENV_DIR = os.path.join(BASE_DIR, "env")
ARTIFACTS_DIR = os.path.join(BASE_DIR, "artifacts")

for _path in (BASE_DIR, INVENTORY_DIR, PROJECT_DIR, ENV_DIR, ARTIFACTS_DIR):
    os.makedirs(_path, exist_ok=True)
