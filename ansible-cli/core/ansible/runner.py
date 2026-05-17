from __future__ import annotations

from typing import Any

import ansible_runner
import core.util.dir as dir


class AnsibleRunError(RuntimeError):
    pass


def run_playbook(
    playbook: str, extravars: dict[str, Any] | None = None
) -> tuple[bool, str]:
    r = ansible_runner.run(
        private_data_dir=dir.BASE_DIR,
        playbook=playbook,
        extravars=extravars or {},
    )

    if r.rc == 0:
        return True, "success"

    status = getattr(r, "status", "failed")
    rc = getattr(r, "rc", 1)
    return False, f"ansible status={status} rc={rc}"
