from __future__ import annotations

import os
from typing import Any

import ansible_runner
import core.util.dir as dir


def _resolve_verbosity() -> int:
    raw = os.getenv("EVLAB_ANSIBLE_VERBOSITY", "0").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(0, min(4, value))


def run_playbook(
    playbook: str, extravars: dict[str, Any] | None = None
) -> tuple[bool, str]:
    effective_extravars = extravars or {}

    passwords: dict[str, str] = {}
    become_pass_obj = effective_extravars.get(
        "ansible_become_pass"
    ) or effective_extravars.get("ansible_become_password")
    if isinstance(become_pass_obj, str) and become_pass_obj:
        passwords[r"(?i).*sudo.*password.*:"] = become_pass_obj
        passwords[r"(?i).*become.*password.*:"] = become_pass_obj

    r = ansible_runner.run(
        private_data_dir=dir.BASE_DIR,
        playbook=playbook,
        extravars=effective_extravars,
        passwords=passwords or None,
        verbosity=_resolve_verbosity(),
    )

    if r.rc == 0:
        return True, "success"

    status = getattr(r, "status", "failed")
    rc = getattr(r, "rc", 1)
    return False, f"ansible status={status} rc={rc}"
