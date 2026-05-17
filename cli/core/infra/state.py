from __future__ import annotations

import os
from typing import Any

import core.util.dir as dir
from core.util.yaml import EvLabYAML

STATE_FILE = os.path.join(dir.ENV_DIR, "infra_state.yaml")


class InfraState:
    def __init__(self) -> None:
        self.yaml = EvLabYAML(STATE_FILE)
        if not isinstance(self.yaml.data, dict):
            self.yaml.data = {}

        defaults = {
            "provision": {},
            "control_plane": {},
            "lab_hosts": {},
            "terraform": {},
            "lan": {},
        }

        for key, value in defaults.items():
            if key not in self.yaml.data or not isinstance(self.yaml.data[key], dict):
                self.yaml.data[key] = value

        self.yaml.dump()

    @property
    def data(self) -> dict[str, Any]:
        payload = self.yaml.data
        return payload if isinstance(payload, dict) else {}

    def save(self) -> None:
        self.yaml.dump()

    def set_provision_type(self, infra_type: str) -> None:
        self.data["provision"]["type"] = infra_type
        self.save()

    def set_terraform(self, values: dict[str, Any]) -> None:
        self.data["terraform"] = values
        self.save()

    def set_control_plane(self, values: dict[str, Any]) -> None:
        self.data["control_plane"].update(values)
        self.save()

    def upsert_lab_host(self, host_key: str, values: dict[str, Any]) -> None:
        hosts = self.data["lab_hosts"]
        current = hosts.get(host_key)
        if not isinstance(current, dict):
            current = {}
            hosts[host_key] = current
        current.update(values)
        self.save()
