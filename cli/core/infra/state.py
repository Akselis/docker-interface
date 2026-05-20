from __future__ import annotations

import ipaddress
import os
from typing import Any

import core.util.dir as dir
from core.util.yaml import EvLabYAML

STATE_FILE = os.path.join(dir.ENV_DIR, "infra_state.yaml")
SCHEMA_VERSION = 2


class InfraState:
    def __init__(self) -> None:
        self.yaml = EvLabYAML(STATE_FILE)
        if not isinstance(self.yaml.data, dict):
            self.yaml.data = {}

        self._ensure_schema()
        self.yaml.dump()

    @property
    def data(self) -> dict[str, Any]:
        payload = self.yaml.data
        return payload if isinstance(payload, dict) else {}

    def save(self) -> None:
        self.yaml.dump()

    def set_provision_type(self, infra_type: str) -> None:
        self.data.setdefault("provision", {})
        self.data["provision"]["type"] = infra_type
        self.save()

    def set_terraform(self, values: dict[str, Any]) -> None:
        self.data["terraform"] = values
        self.save()

    def upsert_device(
        self,
        *,
        ip_address: str,
        provision_group: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        devices = self._devices()
        key = self._find_device_key(ip_address=ip_address, name=name) or ip_address
        current = devices.get(key)
        if not isinstance(current, dict):
            current = {}
            devices[key] = current

        current["ip_address"] = ip_address
        if isinstance(name, str) and name.strip():
            current["name"] = name.strip()
        elif "name" not in current:
            current["name"] = None

        if isinstance(provision_group, str) and provision_group.strip():
            current["provision_group"] = provision_group.strip()

        return current

    def set_control_plane(
        self,
        *,
        ip_address: str,
        values: dict[str, Any],
        provision_group: str | None = None,
        name: str | None = None,
    ) -> None:
        device = self.upsert_device(
            ip_address=ip_address,
            provision_group=provision_group,
            name=name,
        )
        device["control_plane"] = values
        self.save()

    def set_lab_host(
        self,
        *,
        ip_address: str,
        values: dict[str, Any],
        provision_group: str | None = None,
        name: str | None = None,
    ) -> None:
        device = self.upsert_device(
            ip_address=ip_address,
            provision_group=provision_group,
            name=name,
        )
        device["lab_host"] = values
        self.save()

    def set_lan_for_devices(
        self,
        *,
        devices: list[str],
        ssh_user: str,
        ssh_key_path: str,
        become_password: str,
    ) -> None:
        for host in devices:
            if not isinstance(host, str) or not host:
                continue
            device = self.upsert_device(
                ip_address=host,
                provision_group="lan",
            )
            device["lan"] = {
                "ssh_user": ssh_user,
                "ssh_key_path": ssh_key_path,
                "become_password": become_password,
            }
        self.save()

    def find_device(self, identifier: str) -> tuple[str, dict[str, Any]] | None:
        if not isinstance(identifier, str) or not identifier:
            return None

        devices = self._devices()
        for key, device in devices.items():
            if not isinstance(key, str) or not isinstance(device, dict):
                continue
            if identifier == key:
                return key, device

            ip_address = device.get("ip_address")
            name = device.get("name")
            if identifier == ip_address or identifier == name:
                return key, device

            control_plane = device.get("control_plane")
            if isinstance(control_plane, dict):
                for field in ("target_host", "host", "ansible_host"):
                    value = control_plane.get(field)
                    if isinstance(value, str) and identifier == value:
                        return key, device

            lab_host = device.get("lab_host")
            if isinstance(lab_host, dict):
                for field in (
                    "target_host",
                    "ansible_host",
                    "register_ip",
                    "lab_host_id",
                ):
                    value = lab_host.get(field)
                    if isinstance(value, str) and identifier == value:
                        return key, device

        return None

    def get_control_plane(self) -> dict[str, Any] | None:
        for _key, device in self._devices().items():
            if not isinstance(device, dict):
                continue
            cp = device.get("control_plane")
            if isinstance(cp, dict) and cp:
                return cp
        return None

    def remove_device_keys(self, keys: set[str]) -> None:
        if not keys:
            return
        devices = self._devices()
        for key in keys:
            if key in devices:
                del devices[key]
        self.save()

    def reset(self) -> None:
        self.yaml.data = {
            "schema_version": SCHEMA_VERSION,
            "provision": {},
            "devices": {},
            "terraform": {},
        }
        self.save()

    def _ensure_schema(self) -> None:
        payload = self.data
        schema_version = payload.get("schema_version")

        needs_migration = (
            not isinstance(schema_version, int)
            or schema_version < SCHEMA_VERSION
            or "devices" not in payload
            or not isinstance(payload.get("devices"), dict)
        )

        if needs_migration:
            payload = self._migrate_legacy_payload(payload)
            self.yaml.data = payload

        if "provision" not in self.yaml.data or not isinstance(
            self.yaml.data.get("provision"), dict
        ):
            self.yaml.data["provision"] = {}

        if "devices" not in self.yaml.data or not isinstance(
            self.yaml.data.get("devices"), dict
        ):
            self.yaml.data["devices"] = {}

        if "terraform" not in self.yaml.data or not isinstance(
            self.yaml.data.get("terraform"), dict
        ):
            self.yaml.data["terraform"] = {}

        self.yaml.data["schema_version"] = SCHEMA_VERSION

    def _devices(self) -> dict[str, Any]:
        devices = self.data.get("devices")
        if not isinstance(devices, dict):
            self.data["devices"] = {}
            devices = self.data["devices"]
        return devices

    def _find_device_key(self, *, ip_address: str, name: str | None) -> str | None:
        for key, device in self._devices().items():
            if not isinstance(key, str) or not isinstance(device, dict):
                continue
            if device.get("ip_address") == ip_address:
                return key
            if isinstance(name, str) and name and device.get("name") == name:
                return key
        return None

    def _migrate_legacy_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        provision = (
            payload.get("provision")
            if isinstance(payload.get("provision"), dict)
            else {}
        )
        terraform = (
            payload.get("terraform")
            if isinstance(payload.get("terraform"), dict)
            else {}
        )

        migrated: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "provision": provision,
            "devices": {},
            "terraform": terraform,
        }

        provision_type_obj = (
            provision.get("type") if isinstance(provision, dict) else None
        )
        provision_type = (
            provision_type_obj
            if isinstance(provision_type_obj, str) and provision_type_obj
            else None
        )

        def upsert_device(ip_address: str, name: str | None = None) -> dict[str, Any]:
            key = ip_address
            devices = migrated["devices"]
            current = devices.get(key)
            if not isinstance(current, dict):
                current = {
                    "ip_address": ip_address,
                    "name": name,
                }
                devices[key] = current
            else:
                current["ip_address"] = ip_address
                if isinstance(name, str) and name:
                    current["name"] = name

            if isinstance(provision_type, str):
                current.setdefault("provision_group", provision_type)
            return current

        control_plane = payload.get("control_plane")
        if isinstance(control_plane, dict) and control_plane:
            cp_ip = (
                _first_str(
                    control_plane.get("ansible_host"),
                    control_plane.get("host"),
                    control_plane.get("target_host"),
                )
                or "control-plane"
            )
            cp_name_raw = _first_str(control_plane.get("target_host"))
            cp_name = cp_name_raw if cp_name_raw and not _is_ip(cp_name_raw) else None
            device = upsert_device(cp_ip, cp_name)
            device["control_plane"] = control_plane

        lab_hosts = payload.get("lab_hosts")
        if isinstance(lab_hosts, dict):
            for host_key, host_value in lab_hosts.items():
                if not isinstance(host_value, dict):
                    continue

                lab_ip = _first_str(
                    host_value.get("ansible_host"),
                    host_value.get("register_ip"),
                    host_value.get("target_host"),
                    host_key if isinstance(host_key, str) else None,
                )
                if not isinstance(lab_ip, str) or not lab_ip:
                    continue

                lab_name_raw = _first_str(host_value.get("target_host"))
                lab_name = (
                    lab_name_raw
                    if isinstance(lab_name_raw, str)
                    and lab_name_raw
                    and not _is_ip(lab_name_raw)
                    else None
                )
                device = upsert_device(lab_ip, lab_name)
                device["lab_host"] = host_value

        lan = payload.get("lan")
        if isinstance(lan, dict):
            devices_obj = lan.get("devices")
            lan_devices = devices_obj if isinstance(devices_obj, list) else []
            lan_payload = {
                "ssh_user": lan.get("ssh_user"),
                "ssh_key_path": lan.get("ssh_key_path"),
                "become_password": lan.get("become_password"),
            }
            for item in lan_devices:
                if not isinstance(item, str) or not item:
                    continue
                device = upsert_device(item)
                device["provision_group"] = "lan"
                device["lan"] = lan_payload

        return migrated


def _first_str(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False
