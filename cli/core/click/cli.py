from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from typing import Any
from urllib import error, parse, request

import click
import core.ansible.inventory as inv
import core.util.dir as dir
from core.ansible.runner import run_playbook
from core.infra.state import InfraState

CONTROL_PLANE_DEFAULT_IMAGE = "ghcr.io/akselis/control-plane:latest"
LAB_HOST_DEFAULT_IMAGE = "ghcr.io/akselis/lab-host:latest"
INGRESS_DEFAULT_IMAGE = "traefik:v3.1"
CONTROL_PLANE_CONTAINER_NAME = "control-plane-api"


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _is_loopback_host(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered == "localhost":
        return True

    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _detect_control_plane_gateway_ip(
    container_name: str = CONTROL_PLANE_CONTAINER_NAME,
) -> str | None:
    inspect_cmd = [
        "docker",
        "inspect",
        container_name,
        "--format",
        "{{range $name, $network := .NetworkSettings.Networks}}{{if $network.Gateway}}{{$network.Gateway}} {{end}}{{end}}",
    ]
    try:
        result = subprocess.run(
            inspect_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    if result.returncode != 0:
        return None

    for token in result.stdout.split():
        if _is_ip(token) and not _is_loopback_host(token):
            return token

    return None


def _rewrite_url_host_if_loopback(url: str, replacement_host: str) -> str:
    try:
        parsed_url = parse.urlsplit(url)
    except Exception:
        return url

    original_host = parsed_url.hostname
    if not isinstance(original_host, str) or not _is_loopback_host(original_host):
        return url

    userinfo = ""
    if parsed_url.username:
        userinfo = parsed_url.username
        if parsed_url.password is not None:
            userinfo = f"{userinfo}:{parsed_url.password}"
        userinfo = f"{userinfo}@"

    port_part = f":{parsed_url.port}" if parsed_url.port is not None else ""
    new_netloc = f"{userinfo}{replacement_host}{port_part}"

    return parse.urlunsplit(
        (
            parsed_url.scheme,
            new_netloc,
            parsed_url.path,
            parsed_url.query,
            parsed_url.fragment,
        )
    )


def _iter_group_hosts(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hosts: dict[str, dict[str, Any]] = {}
    for group_obj in data.values():
        if not isinstance(group_obj, dict):
            continue
        group_hosts = group_obj.get("hosts")
        if isinstance(group_hosts, dict):
            for host_name, host_vars in group_hosts.items():
                hosts[str(host_name)] = host_vars if isinstance(host_vars, dict) else {}
    return hosts


def _resolve_device(i: inv.EvLabInventory, device: str) -> tuple[str, dict[str, Any]]:
    data = i.yaml.data if isinstance(i.yaml.data, dict) else {}
    hosts = _iter_group_hosts(data)

    if device in hosts:
        return device, hosts[device]

    for host_name, host_vars in hosts.items():
        ansible_host = host_vars.get("ansible_host")
        if isinstance(ansible_host, str) and ansible_host == device:
            return host_name, host_vars

    if _is_ip(device):
        return device, {"ansible_host": device}

    if device == "localhost":
        return "localhost", {"ansible_connection": "local", "ansible_host": "127.0.0.1"}

    raise click.ClickException(f"Device not found in inventory and not an IP: {device}")


def _ensure_inventory_host(group: str, device: str) -> tuple[str, dict[str, Any]]:
    i = inv.EvLabInventory()
    try:
        host_name, host_vars = _resolve_device(i, device)
    except click.ClickException:
        host_name = device
        if device == "localhost":
            host_vars = {"ansible_connection": "local", "ansible_host": "127.0.0.1"}
        elif _is_ip(device):
            host_vars = {"ansible_host": device}
        else:
            raise

    i.group_insert(group)
    i.host_insert_update(host_name=host_name, group_name=group, host_vars=host_vars)
    return host_name, host_vars


def _load_terraform_output(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}


def _extract_ip_addresses_from_payload(payload: object) -> list[str]:
    found: set[str] = set()

    def _visit(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                _visit(item)
            return
        if isinstance(value, list):
            for item in value:
                _visit(item)
            return
        if isinstance(value, str):
            candidate = value.strip()
            if _is_ip(candidate):
                found.add(candidate)

    _visit(payload)
    return sorted(found)


def _ensure_local_ssh_keypair(private_key_path: str) -> tuple[str, str]:
    private_key = os.path.expanduser(private_key_path)
    public_key = f"{private_key}.pub"

    os.makedirs(os.path.dirname(private_key), exist_ok=True)

    if os.path.exists(private_key) and os.path.exists(public_key):
        return private_key, public_key

    subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-f",
            private_key,
            "-N",
            "",
            "-q",
        ],
        check=True,
    )
    return private_key, public_key


def _parse_devices_csv(devices: str) -> list[str]:
    return [item.strip() for item in devices.split(",") if item.strip()]


def _state_devices(state: InfraState) -> dict[str, Any]:
    devices = state.data.get("devices") if isinstance(state.data, dict) else None
    return devices if isinstance(devices, dict) else {}


def _state_device_identifiers(device_key: str, device: dict[str, Any]) -> set[str]:
    identifiers: set[str] = {device_key}

    for field in ("ip_address", "name"):
        value = device.get(field)
        if isinstance(value, str) and value:
            identifiers.add(value)

    control_plane = device.get("control_plane")
    if isinstance(control_plane, dict):
        for field in ("target_host", "host", "ansible_host"):
            value = control_plane.get(field)
            if isinstance(value, str) and value:
                identifiers.add(value)

    lab_host = device.get("lab_host")
    if isinstance(lab_host, dict):
        for field in ("target_host", "ansible_host", "register_ip", "lab_host_id"):
            value = lab_host.get(field)
            if isinstance(value, str) and value:
                identifiers.add(value)

    return identifiers


def _state_first_lan_value(state: InfraState, key: str) -> str | None:
    for _device_key, device in _state_devices(state).items():
        if not isinstance(device, dict):
            continue
        lan_obj = device.get("lan")
        if not isinstance(lan_obj, dict):
            continue
        value = lan_obj.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_become_password(
    provided_become_password: str | None,
    state: InfraState | None = None,
) -> str:
    if isinstance(provided_become_password, str) and provided_become_password:
        return provided_become_password

    if isinstance(state, InfraState):
        stored = _state_first_lan_value(state, "become_password")
        if isinstance(stored, str) and stored:
            click.secho(
                "Using stored LAN sudo password from CLI state. "
                "Override with --become-password if needed.",
                fg="yellow",
            )
            return stored

    return click.prompt("Sudo password", hide_input=True, type=str)


def _raise_with_sudo_hint(prefix: str, ansible_msg: str) -> None:
    lowered = ansible_msg.lower()
    if "waiting for privilege escalation prompt" in lowered:
        raise click.ClickException(
            f"{prefix}: {ansible_msg}. "
            "Sudo escalation is failing on the target host. "
            "Verify the sudo password/user on the host (sudo -k && sudo -v), "
            "or pass --become-password explicitly."
        )

    raise click.ClickException(f"{prefix}: {ansible_msg}")


def _slugify_for_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)


def _collect_context_hosts(state: InfraState) -> set[str]:
    hosts: set[str] = set()

    for device_key, device in _state_devices(state).items():
        if not isinstance(device_key, str) or not isinstance(device, dict):
            continue
        hosts.update(_state_device_identifiers(device_key, device))

    return hosts


def _remove_hosts_from_inventory(hosts_to_remove: set[str]) -> None:
    if not hosts_to_remove:
        return

    i = inv.EvLabInventory()
    data = i.yaml.data if isinstance(i.yaml.data, dict) else {}

    for group_data in data.values():
        if not isinstance(group_data, dict):
            continue
        group_hosts = group_data.get("hosts")
        if not isinstance(group_hosts, dict):
            continue

        for host_name in list(group_hosts.keys()):
            host_vars_obj = group_hosts.get(host_name)
            host_vars = host_vars_obj if isinstance(host_vars_obj, dict) else {}
            ansible_host = host_vars.get("ansible_host")

            if host_name in hosts_to_remove or (
                isinstance(ansible_host, str) and ansible_host in hosts_to_remove
            ):
                del group_hosts[host_name]

    i.yaml.dump()


def _set_inventory_ssh_key_for_hosts(
    hosts: set[str],
    ssh_key_path: str,
) -> None:
    if not hosts:
        return

    resolved_key_path = os.path.expanduser(ssh_key_path)

    i = inv.EvLabInventory()
    data = i.yaml.data if isinstance(i.yaml.data, dict) else {}

    for group_data in data.values():
        if not isinstance(group_data, dict):
            continue
        group_hosts = group_data.get("hosts")
        if not isinstance(group_hosts, dict):
            continue

        for host_name, host_vars_obj in group_hosts.items():
            host_vars = host_vars_obj if isinstance(host_vars_obj, dict) else {}
            ansible_host_obj = host_vars.get("ansible_host")
            ansible_host = (
                ansible_host_obj if isinstance(ansible_host_obj, str) else None
            )
            if host_name not in hosts and (
                not isinstance(ansible_host, str) or ansible_host not in hosts
            ):
                continue
            host_vars["ansible_ssh_private_key_file"] = resolved_key_path
            host_vars.setdefault(
                "ansible_ssh_common_args",
                "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
            )
            group_hosts[host_name] = host_vars

    i.yaml.dump()


def _expand_host_identifiers(state: InfraState, hosts: set[str]) -> set[str]:
    expanded = {item for item in hosts if isinstance(item, str) and item}
    if not expanded:
        return expanded

    inventory = inv.EvLabInventory()
    data = inventory.yaml.data if isinstance(inventory.yaml.data, dict) else {}
    for group_data in data.values():
        if not isinstance(group_data, dict):
            continue
        group_hosts = group_data.get("hosts")
        if not isinstance(group_hosts, dict):
            continue
        for host_name, host_vars_obj in group_hosts.items():
            if not isinstance(host_name, str):
                continue
            host_vars = host_vars_obj if isinstance(host_vars_obj, dict) else {}
            ansible_host = host_vars.get("ansible_host")
            if host_name in expanded or (
                isinstance(ansible_host, str) and ansible_host in expanded
            ):
                expanded.add(host_name)
                if isinstance(ansible_host, str) and ansible_host:
                    expanded.add(ansible_host)

    for device_key, device in _state_devices(state).items():
        if not isinstance(device_key, str) or not isinstance(device, dict):
            continue
        identifiers = _state_device_identifiers(device_key, device)
        if expanded.intersection(identifiers):
            expanded.update(identifiers)

    return expanded


def _remove_hosts_from_state(state: InfraState, hosts_to_remove: set[str]) -> None:
    if not hosts_to_remove:
        return

    expanded_hosts = _expand_host_identifiers(state, hosts_to_remove)
    keys_to_remove: set[str] = set()

    for device_key, device in _state_devices(state).items():
        if not isinstance(device_key, str) or not isinstance(device, dict):
            continue
        identifiers = _state_device_identifiers(device_key, device)
        if expanded_hosts.intersection(identifiers):
            keys_to_remove.add(device_key)

    state.remove_device_keys(keys_to_remove)


def _resolve_destroy_hosts(state: InfraState, device: str | None) -> set[str]:
    context_hosts = _collect_context_hosts(state)
    if not device:
        return context_hosts

    i = inv.EvLabInventory()
    try:
        host_name, host_vars = _resolve_device(i, device)
        resolved = {host_name}
        ansible_host_obj = host_vars.get("ansible_host")
        if isinstance(ansible_host_obj, str) and ansible_host_obj:
            resolved.add(ansible_host_obj)
        return resolved
    except click.ClickException:
        matched = state.find_device(device)
        if matched is not None:
            device_key, device_obj = matched
            if isinstance(device_obj, dict):
                return _state_device_identifiers(device_key, device_obj)

        if device in context_hosts:
            return {device}
        raise click.ClickException(f"Device not found in CLI context: {device}")


def _resolve_effective_ssh_key_path(
    *,
    state: InfraState,
    explicit_ssh_key_path: str | None,
    host_vars: dict[str, Any] | None = None,
) -> str | None:
    def _existing_path(candidate: str | None) -> str | None:
        if not isinstance(candidate, str) or not candidate.strip():
            return None
        resolved = os.path.expanduser(candidate.strip())
        return resolved if os.path.exists(resolved) else None

    if isinstance(explicit_ssh_key_path, str) and explicit_ssh_key_path.strip():
        resolved = _existing_path(explicit_ssh_key_path)
        if resolved is None:
            raise click.ClickException(
                f"SSH key path does not exist: {os.path.expanduser(explicit_ssh_key_path.strip())}"
            )
        return resolved

    inventory_key = None
    if isinstance(host_vars, dict):
        key_obj = host_vars.get("ansible_ssh_private_key_file")
        inventory_key = key_obj if isinstance(key_obj, str) else None
        resolved_inventory = _existing_path(inventory_key)
        if resolved_inventory is not None:
            return resolved_inventory

    lan_key = _state_first_lan_value(state, "ssh_key_path")
    resolved_lan = _existing_path(lan_key)
    if resolved_lan is not None:
        click.secho(
            f"Using LAN ssh_key_path from state: {resolved_lan}",
            fg="yellow",
        )
        return resolved_lan

    if isinstance(inventory_key, str) and inventory_key.strip():
        raise click.ClickException(
            "Inventory SSH key path is set but not accessible in current runtime: "
            f"{os.path.expanduser(inventory_key)}. "
            "Pass --ssh-key-path explicitly (or ensure that key file exists/mounted)."
        )

    return None


def _control_plane_state_or_fail(state: InfraState) -> dict[str, Any]:
    cp_state = state.get_control_plane()
    if not isinstance(cp_state, dict) or not cp_state:
        raise click.ClickException(
            "Control plane state is missing. Bootstrap control-plane first."
        )
    return cp_state


def _resolve_control_plane_host_id(state: InfraState, host_ref: str) -> int:
    ref = host_ref.strip()
    if not ref:
        raise click.ClickException("--host-id cannot be empty")

    if ref.isdigit():
        host_id = int(ref)
        if host_id <= 0:
            raise click.ClickException("--host-id must be a positive integer")
        return host_id

    for device_key, device in _state_devices(state).items():
        if not isinstance(device_key, str) or not isinstance(device, dict):
            continue
        lab_host_obj = device.get("lab_host")
        if not isinstance(lab_host_obj, dict):
            continue

        candidates = _state_device_identifiers(device_key, device)
        response_obj = lab_host_obj.get("register_response")
        if isinstance(response_obj, dict):
            host_obj = response_obj.get("host")
            if isinstance(host_obj, dict):
                hostname = host_obj.get("hostname")
                ip_address = host_obj.get("ip_address")
                host_id_obj = host_obj.get("id")

                if isinstance(hostname, str) and hostname:
                    candidates.add(hostname)
                if isinstance(ip_address, str) and ip_address:
                    candidates.add(ip_address)

                if ref in candidates and isinstance(host_id_obj, int):
                    return host_id_obj

    raise click.ClickException(
        "Could not resolve --host-id. Use numeric control-plane host id or a known "
        "registered lab host reference (lab_host_id/hostname/ip)."
    )


def _call_control_plane_api(
    control_plane: dict[str, Any],
    method: str,
    endpoint_path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    control_plane_host = control_plane.get("host") or control_plane.get("ansible_host")
    control_plane_port = int(control_plane.get("port", 8001))
    control_plane_scheme = str(control_plane.get("scheme", "http"))
    control_plane_api_key = control_plane.get("api_key")

    if not isinstance(control_plane_host, str) or not control_plane_host:
        raise click.ClickException("Control plane host is not configured in state")
    if not isinstance(control_plane_api_key, str) or not control_plane_api_key:
        raise click.ClickException("Control plane API key is not configured in state")

    path = endpoint_path if endpoint_path.startswith("/") else f"/{endpoint_path}"
    url = f"{control_plane_scheme}://{control_plane_host}:{control_plane_port}{path}"
    if query:
        url = f"{url}?{parse.urlencode(query)}"

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": control_plane_api_key,
        },
        method=method,
    )

    try:
        with request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(
            f"Control plane request failed. {method} {path} HTTP {exc.code}: {detail}"
        ) from exc
    except error.URLError as exc:
        raise click.ClickException(f"Failed to reach control plane: {exc}") from exc


def _call_lab_host_api(
    *,
    host: str,
    port: int,
    scheme: str,
    api_key: str,
    method: str,
    endpoint_path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    path = endpoint_path if endpoint_path.startswith("/") else f"/{endpoint_path}"
    url = f"{scheme}://{host}:{port}{path}"
    if query:
        url = f"{url}?{parse.urlencode(query)}"

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method=method,
    )

    try:
        with request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(
            f"Lab-host request failed. {method} {path} HTTP {exc.code}: {detail}"
        ) from exc
    except error.URLError as exc:
        raise click.ClickException(f"Failed to reach lab-host: {exc}") from exc


def _register_lab_host(
    control_plane: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    return _call_control_plane_api(
        control_plane=control_plane,
        method="POST",
        endpoint_path="/hosts/register",
        payload=payload,
    )


def _destroy_all_labs_via_control_plane(control_plane: dict[str, Any]) -> None:
    labs_response = _call_control_plane_api(control_plane, "GET", "/labs")
    labs_obj = labs_response.get("labs") if isinstance(labs_response, dict) else None
    labs = labs_obj if isinstance(labs_obj, list) else []

    for lab in labs:
        if not isinstance(lab, dict):
            continue
        lab_name = lab.get("name")
        if not isinstance(lab_name, str) or not lab_name:
            continue
        _call_control_plane_api(control_plane, "DELETE", f"/labs/{lab_name}")


def _prune_and_remove_hosts_via_control_plane(control_plane: dict[str, Any]) -> None:
    hosts_response = _call_control_plane_api(control_plane, "GET", "/hosts")
    hosts_obj = (
        hosts_response.get("hosts") if isinstance(hosts_response, dict) else None
    )
    hosts = hosts_obj if isinstance(hosts_obj, list) else []

    for host in hosts:
        if not isinstance(host, dict):
            continue
        host_id = host.get("id")
        if not isinstance(host_id, int):
            continue

        for endpoint in ("/containers/prune", "/networks/prune", "/volumes/prune"):
            _call_control_plane_api(
                control_plane,
                "POST",
                f"/hosts/{host_id}/call",
                payload={
                    "method": "POST",
                    "endpoint_path": endpoint,
                },
            )

    for host in hosts:
        if not isinstance(host, dict):
            continue
        host_id = host.get("id")
        if not isinstance(host_id, int):
            continue
        _call_control_plane_api(control_plane, "DELETE", f"/hosts/{host_id}")


def _device_keys_for_identifiers(
    state: InfraState,
    identifiers: set[str],
) -> set[str]:
    expanded_hosts = _expand_host_identifiers(state, identifiers)
    keys: set[str] = set()
    for device_key, device in _state_devices(state).items():
        if not isinstance(device_key, str) or not isinstance(device, dict):
            continue
        if expanded_hosts.intersection(_state_device_identifiers(device_key, device)):
            keys.add(device_key)
    return keys


def _device_target_host(device: dict[str, Any], fallback_key: str) -> str:
    control_plane = device.get("control_plane")
    if isinstance(control_plane, dict):
        for key in ("target_host", "ansible_host", "host"):
            value = control_plane.get(key)
            if isinstance(value, str) and value:
                return value

    lab_host = device.get("lab_host")
    if isinstance(lab_host, dict):
        for key in ("target_host", "ansible_host", "register_ip"):
            value = lab_host.get(key)
            if isinstance(value, str) and value:
                return value

    ip_obj = device.get("ip_address")
    if isinstance(ip_obj, str) and ip_obj:
        return ip_obj

    name_obj = device.get("name")
    if isinstance(name_obj, str) and name_obj:
        return name_obj

    return fallback_key


def _cleanup_device_local_artifacts(device: dict[str, Any]) -> None:
    lab_host = device.get("lab_host")
    if not isinstance(lab_host, dict):
        return

    artifact_keys = ("lab_host_id_artifact", "lab_host_port_artifact")
    for key in artifact_keys:
        value = lab_host.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            os.remove(value)
        except FileNotFoundError:
            continue
        except OSError:
            continue


def _clear_local_cli_runtime_data() -> None:
    for runtime_dir in (dir.ARTIFACTS_DIR, dir.ENV_DIR, dir.INVENTORY_DIR):
        for entry in os.listdir(runtime_dir):
            path = os.path.join(runtime_dir, entry)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except OSError:
                continue


def _ensure_sudo_compatibility(
    *,
    target_host: str,
    sudo_password: str,
    ssh_key_path: str | None = None,
) -> None:
    extravars: dict[str, Any] = {
        "target_host": target_host,
        "sudo_password": sudo_password,
    }
    if isinstance(ssh_key_path, str) and ssh_key_path:
        extravars["ansible_ssh_private_key_file"] = os.path.expanduser(ssh_key_path)
        extravars.setdefault(
            "ansible_ssh_common_args",
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        )

    ok, msg = run_playbook("host.ensure.sudo_compat.yaml", extravars)
    if not ok:
        _raise_with_sudo_hint("Sudo compatibility check failed", msg)


def _ensure_firewall_ports(
    *,
    target_host: str,
    become_password: str,
    allow_tcp_ports: list[int],
) -> None:
    ports = sorted({int(p) for p in allow_tcp_ports if int(p) > 0})
    if not ports:
        return

    if _is_loopback_host(target_host) and os.path.exists("/.dockerenv"):
        click.secho(
            "Skipping firewall automation for containerized localhost target.",
            fg="yellow",
        )
        return

    ok, msg = run_playbook(
        "host.ensure.firewall.yaml",
        {
            "target_host": target_host,
            "ansible_become_password": become_password,
            "ansible_become_pass": become_password,
            "ansible_become_flags": "-H -S",
            "firewall_allow_tcp_ports": ports,
        },
    )
    if not ok:
        _raise_with_sudo_hint("Firewall setup failed", msg)


def _load_json_from_args(
    json_str: str | None, json_file: str | None
) -> dict[str, Any] | None:
    if json_str and json_file:
        raise click.ClickException("Use either --json or --json-file, not both")

    payload_text: str | None = json_str
    if json_file:
        with open(json_file, "r", encoding="utf-8") as f:
            payload_text = f.read()

    if payload_text is None:
        return None

    try:
        parsed = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid JSON payload: {exc}") from exc

    if not isinstance(parsed, dict):
        raise click.ClickException("JSON payload must be an object")

    return parsed


def _load_text_from_path(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise click.ClickException(f"Could not read file '{path}': {exc}") from exc


def _parse_key_value_pairs(
    values: tuple[str, ...], *, option_name: str
) -> dict[str, str] | None:
    if not values:
        return None

    parsed: dict[str, str] = {}
    for raw_item in values:
        item = raw_item.strip()
        if not item:
            continue
        if "=" not in item:
            raise click.ClickException(
                f"Invalid {option_name} entry '{raw_item}'. Use KEY=VALUE format."
            )
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise click.ClickException(
                f"Invalid {option_name} entry '{raw_item}'. Key cannot be empty."
            )
        parsed[key] = value

    return parsed or None


def _parse_csv_items(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


@click.group()
def cli() -> None:
    pass


@cli.group()
@click.option(
    "-v",
    "--ansible-verbose",
    count=True,
    help="Increase ansible verbosity (-v up to -vvvv) for infra commands",
)
@click.pass_context
def infra(ctx: click.Context, ansible_verbose: int) -> None:
    """Infrastructure lifecycle commands."""
    if ansible_verbose > 0:
        os.environ["EVLAB_ANSIBLE_VERBOSITY"] = str(min(ansible_verbose, 4))
    else:
        os.environ["EVLAB_ANSIBLE_VERBOSITY"] = "0"


@infra.command("provision")
@click.option(
    "--type", "infra_type", type=click.Choice(["local", "lan", "cloud"]), required=True
)
@click.option("--terraform-workdir", default=".", show_default=True)
@click.option("--terraform-workspace", default="default", show_default=True)
@click.option("--terraform-var-file", default="", show_default=True)
@click.option("--auto-approve/--no-auto-approve", default=True, show_default=True)
@click.option("--devices", default="", help="Comma-separated LAN device IPs/hostnames")
@click.option("--name", "device_name", default=None, help="Optional device alias")
@click.option("--ssh-user", default="", help="SSH username for LAN bootstrap")
@click.option("--ssh-key-path", default="~/.ssh/evlab_ed25519", show_default=True)
def infra_provision(
    infra_type: str,
    terraform_workdir: str,
    terraform_workspace: str,
    terraform_var_file: str,
    auto_approve: bool,
    devices: str,
    device_name: str | None,
    ssh_user: str,
    ssh_key_path: str,
) -> None:
    state = InfraState()

    if infra_type == "local":
        i = inv.EvLabInventory()
        i.group_insert("local")
        effective_name = (
            device_name.strip()
            if isinstance(device_name, str) and device_name.strip()
            else "localhost"
        )
        i.host_insert_update(
            host_name=effective_name,
            group_name="local",
            host_vars={"ansible_connection": "local", "ansible_host": "127.0.0.1"},
        )
        state.set_provision_type(infra_type)
        state.upsert_device(
            ip_address="127.0.0.1",
            provision_group="local",
            name=(effective_name if effective_name != "localhost" else None),
        )
        state.save()
        click.secho(
            "Local infrastructure provisioned (inventory prepared).", fg="green"
        )
        return

    if infra_type == "lan":
        device_list = _parse_devices_csv(devices)
        if not device_list:
            raise click.ClickException("--devices is required for --type lan")
        if (
            isinstance(device_name, str)
            and device_name.strip()
            and len(device_list) != 1
        ):
            raise click.ClickException(
                "--name is supported for single-device LAN provision only"
            )

        effective_user = ssh_user.strip() or click.prompt("SSH username", type=str)
        ssh_password = click.prompt("SSH password", hide_input=True, type=str)
        become_password = click.prompt(
            "Sudo password (press Enter if same as SSH password or not needed)",
            hide_input=True,
            default="",
            show_default=False,
            type=str,
        )
        if not become_password:
            become_password = ssh_password

        try:
            private_key, public_key = _ensure_local_ssh_keypair(ssh_key_path)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise click.ClickException(
                f"Failed to generate ssh keypair: {exc}"
            ) from exc

        inventory = inv.EvLabInventory()
        inventory.group_insert("lan")

        provisioned_hosts: set[str] = set()

        for idx, device_entry in enumerate(device_list):
            host_name = (
                device_name.strip()
                if idx == 0 and isinstance(device_name, str) and device_name.strip()
                else device_entry
            )
            host_vars = {
                "ansible_host": device_entry,
                "ansible_user": effective_user,
                "ansible_ssh_private_key_file": private_key,
                "ansible_ssh_common_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
            }
            inventory.host_insert_update(
                host_name=host_name,
                group_name="lan",
                host_vars=host_vars,
            )
            provisioned_hosts.add(host_name)

            ok, msg = run_playbook(
                "lan.ssh.bootstrap.yaml",
                {
                    "target_host": host_name,
                    "bootstrap_user": effective_user,
                    "bootstrap_password": ssh_password,
                    "public_key_path": public_key,
                },
            )
            if not ok:
                _remove_hosts_from_inventory(provisioned_hosts)
                raise click.ClickException(
                    f"Failed to bootstrap ssh key on {device_entry}: {msg}"
                )

            try:
                _ensure_sudo_compatibility(
                    target_host=host_name,
                    sudo_password=become_password,
                    ssh_key_path=private_key,
                )
            except click.ClickException as exc:
                _remove_hosts_from_inventory(provisioned_hosts)
                raise click.ClickException(
                    f"Failed to prepare sudo compatibility on {device_entry}: {exc}"
                ) from exc

        state.set_provision_type(infra_type)
        state.set_lan_for_devices(
            devices=device_list,
            ssh_user=effective_user,
            ssh_key_path=private_key,
            become_password=become_password,
        )
        if (
            isinstance(device_name, str)
            and device_name.strip()
            and len(device_list) == 1
        ):
            state.upsert_device(
                ip_address=device_list[0],
                provision_group="lan",
                name=device_name.strip(),
            )
            state.save()

        click.secho(
            "LAN infrastructure provision complete (SSH keys installed).", fg="green"
        )
        return

    output_file = os.path.join(dir.ARTIFACTS_DIR, "terraform_output.json")
    ok, msg = run_playbook(
        "infra.provision.yaml",
        {
            "terraform_workdir": terraform_workdir,
            "terraform_workspace": terraform_workspace,
            "terraform_var_file": terraform_var_file,
            "terraform_auto_approve": auto_approve,
            "terraform_output_file": output_file,
        },
    )
    if not ok:
        raise click.ClickException(msg)

    tf = _load_terraform_output(output_file)
    state.set_provision_type(infra_type)
    state.set_terraform(
        {
            "workdir": terraform_workdir,
            "workspace": terraform_workspace,
            "var_file": terraform_var_file,
            "output_file": output_file,
            "last_output": tf,
        }
    )

    extracted_ips = _extract_ip_addresses_from_payload(tf)
    for idx, ip in enumerate(extracted_ips):
        device_obj = state.upsert_device(
            ip_address=ip,
            provision_group="cloud",
            name=(
                device_name.strip()
                if idx == 0
                and isinstance(device_name, str)
                and device_name.strip()
                and len(extracted_ips) == 1
                else None
            ),
        )
        if isinstance(device_obj, dict):
            device_obj["terraform"] = {
                "workspace": terraform_workspace,
                "workdir": terraform_workdir,
                "output_file": output_file,
            }

    state.save()
    click.secho("Infrastructure provision complete.", fg="green")


@infra.command("status")
def infra_status() -> None:
    state = InfraState()
    provision_type = state.data.get("provision", {}).get("type")

    if provision_type in {"cloud"}:
        tf_state = (
            state.data.get("terraform", {}) if isinstance(state.data, dict) else {}
        )
        workdir = str(tf_state.get("workdir") or ".")
        workspace = str(tf_state.get("workspace") or "default")
        output_file = str(
            tf_state.get("output_file")
            or os.path.join(dir.ARTIFACTS_DIR, "terraform_output.json")
        )

        ok, msg = run_playbook(
            "infra.status.yaml",
            {
                "terraform_workdir": workdir,
                "terraform_workspace": workspace,
                "terraform_output_file": output_file,
            },
        )
        if not ok:
            raise click.ClickException(msg)

        tf_output = _load_terraform_output(output_file)
        tf_state["last_output"] = tf_output
        state.set_terraform(tf_state)

    terraform_state = (
        state.data.get("terraform") if isinstance(state.data, dict) else None
    )

    devices_payload: list[dict[str, Any]] = []
    for device_key, device in _state_devices(state).items():
        if not isinstance(device_key, str) or not isinstance(device, dict):
            continue

        ip_address_obj = device.get("ip_address")
        ip_address = (
            ip_address_obj
            if isinstance(ip_address_obj, str) and ip_address_obj
            else device_key
        )

        provision_group_obj = device.get("provision_group")
        provision_group = (
            provision_group_obj
            if isinstance(provision_group_obj, str) and provision_group_obj
            else provision_type
        )

        row: dict[str, Any] = {
            "provision_group": provision_group,
            "ip_address": ip_address,
        }

        name_obj = device.get("name")
        if isinstance(name_obj, str) and name_obj:
            row["name"] = name_obj

        control_plane_obj = device.get("control_plane")
        if isinstance(control_plane_obj, dict) and control_plane_obj:
            row["control_plane"] = control_plane_obj

        lab_host_obj = device.get("lab_host")
        if isinstance(lab_host_obj, dict) and lab_host_obj:
            row["lab_host"] = lab_host_obj

        lan_obj = device.get("lan")
        if isinstance(lan_obj, dict) and lan_obj:
            lan_payload = {
                key: value for key, value in lan_obj.items() if key != "become_password"
            }
            if lan_payload:
                row["lan"] = lan_payload

        terraform_obj = device.get("terraform")
        if provision_group == "cloud":
            if isinstance(terraform_obj, dict) and terraform_obj:
                row["terraform"] = terraform_obj
            elif isinstance(terraform_state, dict) and terraform_state:
                row["terraform"] = terraform_state

        devices_payload.append(row)

    if (
        not devices_payload
        and provision_type == "cloud"
        and isinstance(terraform_state, dict)
        and terraform_state
    ):
        devices_payload.append(
            {
                "provision_group": "cloud",
                "ip_address": "",
                "terraform": terraform_state,
            }
        )

    devices_payload.sort(
        key=lambda item: (
            str(item.get("ip_address") or ""),
            str(item.get("name") or ""),
        )
    )
    click.echo(json.dumps({"devices": devices_payload}, indent=2))


@infra.command("destroy")
@click.option("--device", default=None, help="Device name in inventory or raw IP")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip teardown and only remove device(s) from CLI context",
)
@click.option("--keep-volumes/--remove-volumes", default=False, show_default=True)
@click.option(
    "--become-password",
    default=None,
    hide_input=True,
    help="Sudo password for privilege escalation on the target host",
)
@click.option("--terraform-workdir", default=None)
@click.option("--terraform-workspace", default=None)
@click.option("--terraform-var-file", default=None)
@click.option(
    "--ssh-key-path",
    default=None,
    help="Override SSH private key path for target device(s) during teardown",
)
@click.option("--auto-approve/--no-auto-approve", default=True, show_default=True)
def infra_destroy(
    device: str | None,
    force: bool,
    keep_volumes: bool,
    become_password: str | None,
    terraform_workdir: str | None,
    terraform_workspace: str | None,
    terraform_var_file: str | None,
    ssh_key_path: str | None,
    auto_approve: bool,
) -> None:
    state = InfraState()
    tf_state = state.data.get("terraform", {}) if isinstance(state.data, dict) else {}

    workdir = terraform_workdir or str(tf_state.get("workdir") or ".")
    workspace = terraform_workspace or str(tf_state.get("workspace") or "default")
    var_file = (
        terraform_var_file
        if terraform_var_file is not None
        else str(tf_state.get("var_file") or "")
    )

    target_hosts = _resolve_destroy_hosts(state, device)

    if force:
        _remove_hosts_from_inventory(target_hosts)
        _remove_hosts_from_state(state, target_hosts)
        if target_hosts:
            click.secho(
                f"Force destroy: removed {', '.join(sorted(target_hosts))} from CLI context.",
                fg="yellow",
            )
        else:
            click.secho("Force destroy: no devices found in CLI context.", fg="yellow")
        return

    target_device_keys = _device_keys_for_identifiers(state, target_hosts)
    target_devices = {
        key: value
        for key, value in _state_devices(state).items()
        if isinstance(key, str)
        and key in target_device_keys
        and isinstance(value, dict)
    }

    destroyed_control_plane = False

    if target_hosts:
        effective_ssh_key_path: str | None = None
        explicit_ssh_key = isinstance(ssh_key_path, str) and bool(ssh_key_path.strip())

        if explicit_ssh_key:
            effective_ssh_key_path = ssh_key_path.strip()
        else:
            lan_ssh_key = _state_first_lan_value(state, "ssh_key_path")
            if isinstance(lan_ssh_key, str) and lan_ssh_key:
                effective_ssh_key_path = lan_ssh_key
                click.secho(
                    "Using LAN ssh_key_path from state for teardown: "
                    f"{effective_ssh_key_path}",
                    fg="yellow",
                )

        if isinstance(effective_ssh_key_path, str) and effective_ssh_key_path:
            resolved_key = os.path.expanduser(effective_ssh_key_path)
            if os.path.exists(resolved_key):
                _set_inventory_ssh_key_for_hosts(target_hosts, effective_ssh_key_path)
            elif explicit_ssh_key:
                raise click.ClickException(
                    f"SSH key path does not exist: {resolved_key}"
                )
            else:
                click.secho(
                    "LAN ssh_key_path from state does not exist in current runtime: "
                    f"{resolved_key}. Continuing with current inventory SSH settings.",
                    fg="yellow",
                )

        resolved_become_password = _resolve_become_password(become_password, state)
        teardown_extravars: dict[str, Any] = {
            "ansible_become_password": resolved_become_password,
            "ansible_become_pass": resolved_become_password,
            "ansible_become_flags": "-H -S",
        }

        effective_teardown_ssh_key = (
            effective_ssh_key_path
            if isinstance(effective_ssh_key_path, str) and effective_ssh_key_path
            else None
        )

        ordered_device_keys = sorted(
            target_devices.keys(),
            key=lambda item: (
                1 if isinstance(target_devices[item].get("control_plane"), dict) else 0
            ),
        )

        if not ordered_device_keys:
            for target_host in sorted(target_hosts):
                _ensure_sudo_compatibility(
                    target_host=target_host,
                    sudo_password=resolved_become_password,
                    ssh_key_path=effective_teardown_ssh_key,
                )

                ok, msg = run_playbook(
                    "host.teardown.containers.yaml",
                    {
                        **teardown_extravars,
                        "target_host": target_host,
                        "keep_volumes": keep_volumes,
                    },
                )
                if not ok:
                    _raise_with_sudo_hint(
                        f"Container teardown failed on {target_host}",
                        msg,
                    )
        else:
            for device_key in ordered_device_keys:
                device_obj = target_devices[device_key]
                target_host = _device_target_host(device_obj, device_key)

                _ensure_sudo_compatibility(
                    target_host=target_host,
                    sudo_password=resolved_become_password,
                    ssh_key_path=effective_teardown_ssh_key,
                )

                has_lab_host = isinstance(device_obj.get("lab_host"), dict)
                has_control_plane = isinstance(device_obj.get("control_plane"), dict)

                if has_lab_host:
                    ok, msg = run_playbook(
                        "lab_host.teardown.yaml",
                        {
                            **teardown_extravars,
                            "target_host": target_host,
                            "remove_compose_data": not keep_volumes,
                        },
                    )
                    if not ok:
                        _raise_with_sudo_hint(
                            f"Lab-host teardown failed on {target_host}",
                            msg,
                        )

                if has_control_plane:
                    cp_state_obj = device_obj.get("control_plane")
                    if isinstance(cp_state_obj, dict):
                        try:
                            click.secho(
                                "Destroying labs via control-plane API (containers, networks, volumes)...",
                                fg="yellow",
                            )
                            _destroy_all_labs_via_control_plane(cp_state_obj)
                            click.secho(
                                "Pruning lab-host resources via control-plane API...",
                                fg="yellow",
                            )
                            _prune_and_remove_hosts_via_control_plane(cp_state_obj)
                        except click.ClickException as exc:
                            click.secho(
                                "Control-plane cleanup warning: "
                                f"{exc}. Continuing with host teardown.",
                                fg="yellow",
                            )

                    ok, msg = run_playbook(
                        "control_plane.teardown.yaml",
                        {
                            **teardown_extravars,
                            "target_host": target_host,
                            "remove_volumes": True,
                        },
                    )
                    if not ok:
                        _raise_with_sudo_hint(
                            f"Control-plane teardown failed on {target_host}",
                            msg,
                        )
                    destroyed_control_plane = True

                if not has_lab_host and not has_control_plane:
                    ok, msg = run_playbook(
                        "host.teardown.containers.yaml",
                        {
                            **teardown_extravars,
                            "target_host": target_host,
                            "keep_volumes": keep_volumes,
                        },
                    )
                    if not ok:
                        _raise_with_sudo_hint(
                            f"Container teardown failed on {target_host}",
                            msg,
                        )

            for device_obj in target_devices.values():
                if isinstance(device_obj, dict):
                    _cleanup_device_local_artifacts(device_obj)

        _remove_hosts_from_inventory(target_hosts)
        _remove_hosts_from_state(state, target_hosts)

    provision_obj = state.data.get("provision") if isinstance(state.data, dict) else {}
    provision_type = (
        provision_obj.get("type")
        if isinstance(provision_obj, dict)
        and isinstance(provision_obj.get("type"), str)
        else None
    )

    if provision_type not in {"local", "lan"}:
        ok, msg = run_playbook(
            "infra.destroy.yaml",
            {
                "terraform_workdir": workdir,
                "terraform_workspace": workspace,
                "terraform_var_file": var_file,
                "terraform_auto_approve": auto_approve,
            },
        )
        if not ok:
            raise click.ClickException(msg)
        state.set_terraform({})

    if destroyed_control_plane:
        _clear_local_cli_runtime_data()
        state = InfraState()
        state.reset()
        click.secho("Infrastructure destroy complete.", fg="green")
        return

    if provision_type in {"local", "lan"}:
        if target_hosts:
            click.secho("Infrastructure destroy complete.", fg="green")
        else:
            click.secho("No devices found in CLI context to destroy.", fg="yellow")
        return

    click.secho("Infrastructure destroy complete.", fg="green")


@infra.command("remove-device")
@click.option(
    "--device",
    "devices",
    multiple=True,
    required=True,
    help="Device name in inventory or raw IP. Repeat option to remove multiple devices.",
)
def infra_remove_device(devices: tuple[str, ...]) -> None:
    state = InfraState()

    hosts_to_remove: set[str] = set()
    for device in devices:
        resolved = _resolve_destroy_hosts(state, device)
        hosts_to_remove.update(resolved)

    if not hosts_to_remove:
        click.secho("No matching devices found in CLI context.", fg="yellow")
        return

    _remove_hosts_from_inventory(hosts_to_remove)
    _remove_hosts_from_state(state, hosts_to_remove)

    click.secho(
        f"Removed device(s) from CLI context: {', '.join(sorted(hosts_to_remove))}",
        fg="green",
    )


@infra.group("bootstrap")
def infra_bootstrap() -> None:
    """Bootstrap control-plane and lab-host nodes."""


@infra_bootstrap.command("control-plane")
@click.option("--device", required=True, help="Device name in inventory or raw IP")
@click.option("--api-key", "control_plane_api_key", required=True)
@click.option(
    "--image",
    "control_plane_image",
    default=CONTROL_PLANE_DEFAULT_IMAGE,
    show_default=True,
)
@click.option("--port", "control_plane_port", default=8001, type=int, show_default=True)
@click.option(
    "--db-password",
    "control_plane_db_password",
    default="control_plane",
    show_default=True,
)
@click.option("--rabbitmq-user", default="guest", show_default=True)
@click.option("--rabbitmq-password", default="guest", show_default=True)
@click.option(
    "--become-password",
    default=None,
    help="Sudo password for privilege escalation on the target host",
    hide_input=True,
)
@click.option(
    "--ssh-key-path",
    default=None,
    help="Override SSH private key path for bootstrap connection",
)
@click.option(
    "--configure-firewall/--no-configure-firewall",
    default=True,
    show_default=True,
)
def bootstrap_control_plane(
    device: str,
    control_plane_api_key: str,
    control_plane_image: str,
    control_plane_port: int,
    control_plane_db_password: str,
    rabbitmq_user: str,
    rabbitmq_password: str,
    become_password: str | None,
    ssh_key_path: str | None,
    configure_firewall: bool,
) -> None:
    state = InfraState()
    target_host, host_vars = _ensure_inventory_host("control_plane", device)

    resolved_become_password = _resolve_become_password(become_password, state)
    effective_ssh_key_path = _resolve_effective_ssh_key_path(
        state=state,
        explicit_ssh_key_path=ssh_key_path,
        host_vars=host_vars,
    )

    common_extravars: dict[str, Any] = {
        "target_host": target_host,
        "ansible_become_password": resolved_become_password,
        "ansible_become_pass": resolved_become_password,
        "ansible_become_flags": "-H -S",
    }
    if isinstance(effective_ssh_key_path, str) and effective_ssh_key_path:
        common_extravars["ansible_ssh_private_key_file"] = effective_ssh_key_path
        common_extravars.setdefault(
            "ansible_ssh_common_args",
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        )

    _ensure_sudo_compatibility(
        target_host=target_host,
        sudo_password=resolved_become_password,
        ssh_key_path=effective_ssh_key_path,
    )

    ok, msg = run_playbook("host.ensure.docker.yaml", common_extravars)
    if not ok:
        _raise_with_sudo_hint("Docker setup failed", msg)

    provision = state.data.get("provision") if isinstance(state.data, dict) else {}
    provision_type = (
        provision.get("type")
        if isinstance(provision, dict) and isinstance(provision.get("type"), str)
        else "local"
    )

    ports_artifact = os.path.join(
        dir.ARTIFACTS_DIR,
        f"control_plane_ports_{_slugify_for_filename(target_host)}.json",
    )

    deploy_vars = {
        **common_extravars,
        "control_plane_image": control_plane_image,
        "control_plane_api_key": control_plane_api_key,
        "control_plane_container_port": control_plane_port,
        "control_plane_host_port": control_plane_port,
        "control_plane_db_password": control_plane_db_password,
        "control_plane_rabbitmq_user": rabbitmq_user,
        "control_plane_rabbitmq_password": rabbitmq_password,
        "control_plane_deployment_type": provision_type,
        "control_plane_ports_output_file": ports_artifact,
    }

    ok, msg = run_playbook("control_plane.deploy.yaml", deploy_vars)
    if not ok:
        raise click.ClickException(f"Control plane deploy failed: {msg}")

    ansible_host = str(host_vars.get("ansible_host") or target_host)

    effective_control_plane_port = control_plane_port
    effective_rabbitmq_port = 5672
    if os.path.exists(ports_artifact):
        with open(ports_artifact, "r", encoding="utf-8") as f:
            ports_payload = json.load(f)
        if isinstance(ports_payload, dict):
            cp_port_obj = ports_payload.get("control_plane_port")
            rabbit_port_obj = ports_payload.get("rabbitmq_port")
            if isinstance(cp_port_obj, int) and cp_port_obj > 0:
                effective_control_plane_port = cp_port_obj
            if isinstance(rabbit_port_obj, int) and rabbit_port_obj > 0:
                effective_rabbitmq_port = rabbit_port_obj

    if effective_control_plane_port != control_plane_port:
        click.secho(
            f"Requested control-plane port {control_plane_port} is busy; using {effective_control_plane_port}.",
            fg="yellow",
        )
    if effective_rabbitmq_port != 5672:
        click.secho(
            f"Requested RabbitMQ port 5672 is busy; using {effective_rabbitmq_port}.",
            fg="yellow",
        )

    if configure_firewall:
        _ensure_firewall_ports(
            target_host=target_host,
            become_password=resolved_become_password,
            allow_tcp_ports=[effective_control_plane_port, effective_rabbitmq_port],
        )

    rabbitmq_url = f"amqp://{rabbitmq_user}:{rabbitmq_password}@{ansible_host}:{effective_rabbitmq_port}/%2F"

    state.set_control_plane(
        ip_address=ansible_host,
        provision_group=provision_type,
        name=(target_host if target_host != ansible_host else None),
        values={
            "target_host": target_host,
            "host": ansible_host,
            "ansible_host": ansible_host,
            "scheme": "http",
            "port": effective_control_plane_port,
            "api_key": control_plane_api_key,
            "env": {
                "RABBITMQ_URL": rabbitmq_url,
                "RABBITMQ_EXCHANGE": "lab.events",
                "RABBITMQ_QUEUE": "control_plane.heartbeats",
                "RABBITMQ_ROUTING_KEY_PATTERN": "heartbeat.*",
                "DB_URL": f"postgresql+asyncpg://control_plane:{control_plane_db_password}@control-plane-db:5432/control_plane",
                "DOCKER_INTERFACE_API_KEY": control_plane_api_key,
            },
        },
    )

    click.secho("Control plane bootstrap complete.", fg="green")


@infra_bootstrap.command("lab-host")
@click.option("--device", required=True, help="Device name in inventory or raw IP")
@click.option("--api-key", "lab_host_api_key", required=True)
@click.option(
    "--image", "lab_host_image", default=LAB_HOST_DEFAULT_IMAGE, show_default=True
)
@click.option("--port", "lab_host_port", default=8000, type=int, show_default=True)
@click.option("--host-id", "lab_host_id", default=None)
@click.option(
    "--register-ip",
    default=None,
    help="IP/address control-plane should use for this lab host",
)
@click.option("--cpu-total", default=0, type=int, show_default=True)
@click.option("--memory-total-mb", default=0, type=int, show_default=True)
@click.option(
    "--base-domain",
    default=None,
    help="Base domain for lab host ingress, e.g. example.com",
)
@click.option(
    "--dns-zone", default=None, help="DNS zone to update (defaults to --base-domain)"
)
@click.option(
    "--ingress-target",
    default=None,
    help="Ingress endpoint target for wildcard DNS (IP or DNS)",
)
@click.option(
    "--become-password",
    default=None,
    help="Sudo password for privilege escalation on the target host",
    hide_input=True,
)
@click.option(
    "--ssh-key-path",
    default=None,
    help="Override SSH private key path for bootstrap connection",
)
@click.option("--use-routing/--no-use-routing", default=False, show_default=True)
@click.option("--ingress-image", default=INGRESS_DEFAULT_IMAGE, show_default=True)
@click.option("--ingress-http-port", default=80, type=int, show_default=True)
@click.option("--ingress-https-port", default=443, type=int, show_default=True)
@click.option(
    "--configure-firewall/--no-configure-firewall",
    default=True,
    show_default=True,
)
def bootstrap_lab_host(
    device: str,
    lab_host_api_key: str,
    lab_host_image: str,
    lab_host_port: int,
    lab_host_id: str | None,
    register_ip: str | None,
    cpu_total: int,
    memory_total_mb: int,
    base_domain: str | None,
    dns_zone: str | None,
    ingress_target: str | None,
    become_password: str | None,
    ssh_key_path: str | None,
    use_routing: bool,
    ingress_image: str,
    ingress_http_port: int,
    ingress_https_port: int,
    configure_firewall: bool,
) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)

    target_host, host_vars = _ensure_inventory_host("lab_hosts", device)

    resolved_become_password = _resolve_become_password(become_password, state)
    effective_ssh_key_path = _resolve_effective_ssh_key_path(
        state=state,
        explicit_ssh_key_path=ssh_key_path,
        host_vars=host_vars,
    )

    common_extravars: dict[str, Any] = {
        "target_host": target_host,
        "ansible_become_password": resolved_become_password,
        "ansible_become_pass": resolved_become_password,
        "ansible_become_flags": "-H -S",
    }
    if isinstance(effective_ssh_key_path, str) and effective_ssh_key_path:
        common_extravars["ansible_ssh_private_key_file"] = effective_ssh_key_path
        common_extravars.setdefault(
            "ansible_ssh_common_args",
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        )

    _ensure_sudo_compatibility(
        target_host=target_host,
        sudo_password=resolved_become_password,
        ssh_key_path=effective_ssh_key_path,
    )

    ok, msg = run_playbook("host.ensure.docker.yaml", common_extravars)
    if not ok:
        _raise_with_sudo_hint("Docker setup failed", msg)

    ansible_host = str(host_vars.get("ansible_host") or target_host)
    register_host_value = register_ip or ansible_host

    if register_ip is None and _is_loopback_host(register_host_value):
        detected_register_ip = _detect_control_plane_gateway_ip()
        if isinstance(detected_register_ip, str) and detected_register_ip:
            register_host_value = detected_register_ip
            click.secho(
                "Auto-detected --register-ip from control-plane network gateway: "
                f"{register_host_value}",
                fg="yellow",
            )
        else:
            click.secho(
                "Could not auto-detect non-loopback register IP. "
                "Control-plane may not reach 127.0.0.1 from inside its container. "
                "Pass --register-ip explicitly.",
                fg="yellow",
            )

    rabbitmq_url = (
        cp_state.get("env", {}).get("RABBITMQ_URL")
        if isinstance(cp_state.get("env"), dict)
        else None
    )
    if not isinstance(rabbitmq_url, str) or not rabbitmq_url:
        rabbitmq_url = "amqp://guest:guest@localhost:5672/%2F"

    rewritten_rabbitmq_url = _rewrite_url_host_if_loopback(
        rabbitmq_url,
        register_host_value,
    )
    if rewritten_rabbitmq_url != rabbitmq_url:
        rabbitmq_url = rewritten_rabbitmq_url
        click.secho(
            "Auto-rewrote RABBITMQ_URL host from loopback to "
            f"{register_host_value} for lab-host reachability.",
            fg="yellow",
        )

    register_host_name = lab_host_id or f"lab-host-{target_host}"
    id_artifact = os.path.join(
        dir.ARTIFACTS_DIR,
        f"lab_host_id_{_slugify_for_filename(target_host)}.txt",
    )
    port_artifact = os.path.join(
        dir.ARTIFACTS_DIR,
        f"lab_host_port_{_slugify_for_filename(target_host)}.txt",
    )

    deploy_vars = {
        **common_extravars,
        "lab_host_image": lab_host_image,
        "lab_host_api_key": lab_host_api_key,
        "lab_host_container_port": lab_host_port,
        "lab_host_host_port": lab_host_port,
        "lab_host_rabbitmq_url": rabbitmq_url,
        "lab_host_id_output_file": id_artifact,
        "lab_host_port_output_file": port_artifact,
    }
    if lab_host_id:
        deploy_vars["lab_host_id"] = lab_host_id

    ok, msg = run_playbook("lab_host.deploy.yaml", deploy_vars)
    if not ok:
        raise click.ClickException(f"Lab host deploy failed: {msg}")

    effective_lab_host_port = lab_host_port
    if os.path.exists(port_artifact):
        with open(port_artifact, "r", encoding="utf-8") as f:
            port_text = f.read().strip()
        if port_text.isdigit() and int(port_text) > 0:
            effective_lab_host_port = int(port_text)

    if effective_lab_host_port != lab_host_port:
        click.secho(
            f"Requested lab-host port {lab_host_port} is busy; using {effective_lab_host_port}.",
            fg="yellow",
        )

    effective_ingress_http_port: int | None = None
    effective_ingress_https_port: int | None = None

    if use_routing:
        if not isinstance(base_domain, str) or not base_domain.strip():
            raise click.ClickException(
                "--base-domain is required when --use-routing is enabled"
            )

        ingress_response = _call_lab_host_api(
            host=ansible_host,
            port=effective_lab_host_port,
            scheme="http",
            api_key=lab_host_api_key,
            method="POST",
            endpoint_path="/ingress/ensure",
            payload={
                "image": ingress_image,
                "http_port": ingress_http_port,
                "https_port": ingress_https_port,
                "network_mode": "host",
            },
        )

        ingress_obj = (
            ingress_response.get("ingress")
            if isinstance(ingress_response, dict)
            else None
        )
        ingress_details = (
            ingress_obj.get("ingress") if isinstance(ingress_obj, dict) else None
        )
        if isinstance(ingress_details, dict):
            http_obj = ingress_details.get("http_port")
            https_obj = ingress_details.get("https_port")
            if isinstance(http_obj, int) and http_obj > 0:
                effective_ingress_http_port = http_obj
            if isinstance(https_obj, int) and https_obj > 0:
                effective_ingress_https_port = https_obj

        if (
            effective_ingress_http_port is not None
            and effective_ingress_http_port != ingress_http_port
        ):
            click.secho(
                f"Requested ingress HTTP port {ingress_http_port} is busy; using {effective_ingress_http_port}.",
                fg="yellow",
            )
        if (
            effective_ingress_https_port is not None
            and effective_ingress_https_port != ingress_https_port
        ):
            click.secho(
                f"Requested ingress HTTPS port {ingress_https_port} is busy; using {effective_ingress_https_port}.",
                fg="yellow",
            )

    if configure_firewall:
        firewall_ports = [effective_lab_host_port]
        if use_routing:
            firewall_ports.extend(
                [
                    effective_ingress_http_port or ingress_http_port,
                    effective_ingress_https_port or ingress_https_port,
                ]
            )
        _ensure_firewall_ports(
            target_host=target_host,
            become_password=resolved_become_password,
            allow_tcp_ports=firewall_ports,
        )

    effective_lab_host_id = register_host_name
    if os.path.exists(id_artifact):
        with open(id_artifact, "r", encoding="utf-8") as f:
            artifact_id = f.read().strip()
        if artifact_id:
            effective_lab_host_id = artifact_id

    effective_ingress_target = ingress_target
    if use_routing and (
        not isinstance(effective_ingress_target, str) or not effective_ingress_target
    ):
        effective_ingress_target = register_host_value

    register_payload = {
        "hostname": effective_lab_host_id,
        "ip_address": register_host_value,
        "port": effective_lab_host_port,
        "scheme": "http",
        "status": "online",
        "cpu_total": cpu_total,
        "memory_total_mb": memory_total_mb,
        "api_key": lab_host_api_key,
        "base_domain": base_domain,
        "dns_zone": dns_zone,
        "ingress_target": effective_ingress_target,
    }

    register_response = _register_lab_host(cp_state, register_payload)

    provision = state.data.get("provision") if isinstance(state.data, dict) else {}
    provision_type = (
        provision.get("type")
        if isinstance(provision, dict) and isinstance(provision.get("type"), str)
        else None
    )

    state.set_lab_host(
        ip_address=ansible_host,
        provision_group=provision_type,
        name=(target_host if target_host != ansible_host else None),
        values={
            "target_host": target_host,
            "ansible_host": ansible_host,
            "register_ip": register_host_value,
            "port": effective_lab_host_port,
            "api_key": lab_host_api_key,
            "lab_host_id": effective_lab_host_id,
            "register_response": register_response,
            "rabbitmq_url": rabbitmq_url,
            "lab_host_id_artifact": id_artifact,
            "lab_host_port_artifact": port_artifact,
            "routing_enabled": use_routing,
            "ingress_image": ingress_image if use_routing else None,
            "ingress_http_port": (
                (effective_ingress_http_port or ingress_http_port)
                if use_routing
                else None
            ),
            "ingress_https_port": (
                (effective_ingress_https_port or ingress_https_port)
                if use_routing
                else None
            ),
            "ingress_target": effective_ingress_target,
        },
    )

    click.secho("Lab host bootstrap complete and registered.", fg="green")


@cli.group("cp")
def control_plane() -> None:
    """Control-plane API commands."""


@control_plane.command("health")
def control_plane_health() -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(cp_state, "GET", "/health")
    click.echo(json.dumps(response, indent=2))


@control_plane.command("request")
@click.option(
    "--method",
    required=True,
    type=click.Choice(["GET", "POST", "PUT", "PATCH", "DELETE"]),
)
@click.option("--path", required=True, help="Control-plane endpoint path, e.g. /labs")
@click.option(
    "--query", default="", help="Query string in key=value&key2=value2 format"
)
@click.option("--json", "json_str", default=None, help="Inline JSON object payload")
@click.option("--json-file", default=None, help="Path to JSON object payload file")
def control_plane_request(
    method: str,
    path: str,
    query: str,
    json_str: str | None,
    json_file: str | None,
) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    payload = _load_json_from_args(json_str, json_file)

    query_dict: dict[str, str] = {}
    if query.strip():
        parsed_qs = parse.parse_qs(query, keep_blank_values=True)
        query_dict = {k: v[-1] for k, v in parsed_qs.items() if v}

    response = _call_control_plane_api(
        cp_state,
        method,
        path,
        payload=payload,
        query=query_dict or None,
    )
    click.echo(json.dumps(response, indent=2))


@control_plane.group("labs")
def control_plane_labs() -> None:
    """Lab management commands."""


@control_plane_labs.command("list")
def control_plane_labs_list() -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(cp_state, "GET", "/labs")
    click.echo(json.dumps(response, indent=2))


@control_plane_labs.command("create")
@click.option("--name", required=True)
@click.option(
    "--host-id",
    required=False,
    type=str,
    default=None,
    help="Control-plane host numeric id or known lab-host reference (lab_host_id/hostname/ip). Omit to schedule automatically.",
)
@click.option(
    "--scheduling-method",
    default="least_allocated",
    show_default=True,
    type=click.Choice(["first_fit", "least_allocated"]),
)
@click.option("--cpu-limit", default=None, type=int)
@click.option("--memory-limit-mb", default=None, type=int)
@click.option(
    "--status", default="stopped", type=click.Choice(["running", "stopped", "failed"])
)
def control_plane_labs_create(
    name: str,
    host_id: str | None,
    scheduling_method: str,
    cpu_limit: int | None,
    memory_limit_mb: int | None,
    status: str,
) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)

    payload: dict[str, Any] = {
        "name": name,
        "status": status,
    }
    if cpu_limit is not None:
        payload["cpu_limit"] = cpu_limit
    if memory_limit_mb is not None:
        payload["memory_limit_mb"] = memory_limit_mb

    if isinstance(host_id, str) and host_id.strip():
        resolved_host_id = _resolve_control_plane_host_id(state, host_id)
        payload["host_id"] = resolved_host_id
        response = _call_control_plane_api(cp_state, "POST", "/labs", payload=payload)
    else:
        response = _call_control_plane_api(
            cp_state,
            "POST",
            "/labs/scheduled",
            payload=payload,
            query={"scheduling_method": scheduling_method},
        )

    click.echo(json.dumps(response, indent=2))


@control_plane_labs.command("delete")
@click.option("--name", required=True)
def control_plane_labs_delete(name: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(cp_state, "DELETE", f"/labs/{name}")
    click.echo(json.dumps(response, indent=2))


@control_plane.group("env")
def control_plane_env() -> None:
    """Lab environment (container) commands."""


@control_plane_env.command("list")
@click.option("--lab", "lab_name", required=True)
def control_plane_env_list(lab_name: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(cp_state, "GET", f"/lab/{lab_name}/environments")
    click.echo(json.dumps(response, indent=2))


@control_plane_env.command("get")
@click.option("--lab", "lab_name", required=True)
@click.option("--name", "container_name", required=True)
def control_plane_env_get(lab_name: str, container_name: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(
        cp_state,
        "GET",
        f"/lab/{lab_name}/environments/{container_name}",
    )
    click.echo(json.dumps(response, indent=2))


@control_plane_env.command("deploy")
@click.option("--lab", "lab_name", required=False, default=None)
@click.option(
    "--scheduling-method",
    default="least_allocated",
    show_default=True,
    type=click.Choice(["first_fit", "least_allocated"]),
)
@click.option("--image", required=True)
@click.option("--name", "container_name", required=True)
@click.option(
    "--network-mode",
    required=True,
    type=click.Choice(
        [
            "offline",
            "internal_private",
            "internal_exposed",
            "external_private",
            "external_exposed",
        ]
    ),
)
@click.option(
    "--json",
    "json_str",
    default=None,
    help="Additional JSON object to merge into payload",
)
@click.option(
    "--json-file", default=None, help="JSON file with additional payload fields"
)
def control_plane_env_deploy(
    lab_name: str | None,
    scheduling_method: str,
    image: str,
    container_name: str,
    network_mode: str,
    json_str: str | None,
    json_file: str | None,
) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)

    payload: dict[str, Any] = {
        "image": image,
        "name": container_name,
        "network_mode": network_mode,
    }
    extra = _load_json_from_args(json_str, json_file)
    if extra:
        payload.update(extra)

    if isinstance(lab_name, str) and lab_name:
        response = _call_control_plane_api(
            cp_state,
            "POST",
            f"/lab/{lab_name}/environments",
            payload=payload,
        )
    else:
        response = _call_control_plane_api(
            cp_state,
            "POST",
            "/environments/scheduled",
            payload=payload,
            query={"scheduling_method": scheduling_method},
        )
    click.echo(json.dumps(response, indent=2))


@control_plane_env.command("update")
@click.option("--lab", "lab_name", required=True)
@click.option("--name", "container_name", required=True)
@click.option(
    "--image",
    default=None,
    help="Override image; if omitted, use current runtime image",
)
@click.option(
    "--network-mode",
    required=True,
    type=click.Choice(
        [
            "offline",
            "internal_private",
            "internal_exposed",
            "external_private",
            "external_exposed",
        ]
    ),
)
@click.option(
    "--json",
    "json_str",
    default=None,
    help="Additional JSON object to merge into payload",
)
@click.option(
    "--json-file", default=None, help="JSON file with additional payload fields"
)
def control_plane_env_update(
    lab_name: str,
    container_name: str,
    image: str | None,
    network_mode: str,
    json_str: str | None,
    json_file: str | None,
) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)

    resolved_image = image
    if not isinstance(resolved_image, str) or not resolved_image.strip():
        current = _call_control_plane_api(
            cp_state,
            "GET",
            f"/lab/{lab_name}/environments/{container_name}",
        )
        env_obj = current.get("environment") if isinstance(current, dict) else None
        current_image = env_obj.get("image") if isinstance(env_obj, dict) else None
        if not isinstance(current_image, str) or not current_image:
            raise click.ClickException(
                "Could not determine current image for environment update. Pass --image explicitly."
            )
        resolved_image = current_image

    payload: dict[str, Any] = {
        "image": resolved_image,
        "name": container_name,
        "network_mode": network_mode,
    }
    extra = _load_json_from_args(json_str, json_file)
    if extra:
        payload.update(extra)

    response = _call_control_plane_api(
        cp_state,
        "POST",
        f"/lab/{lab_name}/environments",
        payload=payload,
    )
    click.echo(json.dumps(response, indent=2))


@control_plane_env.command("delete")
@click.option("--lab", "lab_name", required=True)
@click.option("--name", "container_name", required=True)
def control_plane_env_delete(lab_name: str, container_name: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(
        cp_state,
        "DELETE",
        f"/lab/{lab_name}/environments/{container_name}",
    )
    click.echo(json.dumps(response, indent=2))


@control_plane_env.command("delete-all")
@click.option("--lab", "lab_name", required=True)
def control_plane_env_delete_all(lab_name: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(
        cp_state,
        "DELETE",
        f"/lab/{lab_name}/environments",
    )
    click.echo(json.dumps(response, indent=2))


@control_plane_env.command("state")
@click.option("--lab", "lab_name", required=True)
@click.option(
    "--action",
    "action",
    required=True,
    type=click.Choice(["stop", "pause", "unpause", "start"]),
)
@click.option("--names", required=True, help="Comma-separated container names")
def control_plane_env_state(lab_name: str, action: str, names: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    payload = {"names": [n.strip() for n in names.split(",") if n.strip()]}
    response = _call_control_plane_api(
        cp_state,
        "POST",
        f"/lab/{lab_name}/environments/state/{action}",
        payload=payload,
    )
    click.echo(json.dumps(response, indent=2))


@control_plane.group("projects")
def control_plane_projects() -> None:
    """Lab compose project commands."""


@control_plane_projects.command("list")
@click.option("--lab", "lab_name", required=True)
def control_plane_projects_list(lab_name: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(cp_state, "GET", f"/lab/{lab_name}/projects")
    click.echo(json.dumps(response, indent=2))


@control_plane_projects.command("get")
@click.option("--lab", "lab_name", required=True)
@click.option("--name", "project_name", required=True)
def control_plane_projects_get(lab_name: str, project_name: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(
        cp_state,
        "GET",
        f"/lab/{lab_name}/projects/{project_name}",
    )
    click.echo(json.dumps(response, indent=2))


@control_plane_projects.command("deploy")
@click.option("--lab", "lab_name", required=False, default=None)
@click.option(
    "--scheduling-method",
    default="least_allocated",
    show_default=True,
    type=click.Choice(["first_fit", "least_allocated"]),
)
@click.option("--name", "project_name", default=None, help="Compose project name")
@click.option(
    "--source-type",
    "--source",
    "source_type",
    default=None,
    type=click.Choice(["git", "archive", "inline"]),
    help="Compose source type",
)
@click.option(
    "--source-url",
    "--url",
    "source_url",
    default=None,
    help="Source URL for git/archive source types",
)
@click.option("--ref", default=None, help="Git ref/branch/tag/commit")
@click.option(
    "--compose-file",
    default=None,
    help="Path to compose file inside source (defaults to docker-compose.yml)",
)
@click.option(
    "--compose-inline",
    "compose_inline",
    default=None,
    help="Inline compose YAML content (for source type inline)",
)
@click.option(
    "--compose-inline-file",
    default=None,
    help="Path to file with compose YAML content (for source type inline)",
)
@click.option(
    "--env",
    "env_pairs",
    multiple=True,
    help="Compose process environment variable in KEY=VALUE format. Repeatable.",
)
@click.option(
    "--pull/--no-pull",
    default=None,
    show_default=False,
    help="Run docker compose pull before up",
)
@click.option(
    "--build/--no-build",
    default=None,
    show_default=False,
    help="Run docker compose up with --build",
)
@click.option(
    "--network-mode",
    default=None,
    type=click.Choice(
        [
            "internal_private",
            "internal_exposed",
            "external_private",
            "external_exposed",
        ]
    ),
)
@click.option(
    "--exposed-services",
    default=None,
    help="Comma-separated compose service names to expose via ingress",
)
@click.option(
    "--lifetime-type",
    default=None,
    type=click.Choice(["persistent", "ephemeral", "single_use", "session"]),
)
@click.option("--time-to-live-seconds", default=None, type=int)
@click.option("--cpu-limit", default=None, type=float)
@click.option("--memory-limit", default=None)
@click.option("--json", "json_str", default=None, help="Inline JSON object payload")
@click.option("--json-file", default=None, help="Path to JSON object payload file")
def control_plane_projects_deploy(
    lab_name: str | None,
    scheduling_method: str,
    project_name: str | None,
    source_type: str | None,
    source_url: str | None,
    ref: str | None,
    compose_file: str | None,
    compose_inline: str | None,
    compose_inline_file: str | None,
    env_pairs: tuple[str, ...],
    pull: bool | None,
    build: bool | None,
    network_mode: str | None,
    exposed_services: str | None,
    lifetime_type: str | None,
    time_to_live_seconds: int | None,
    cpu_limit: float | None,
    memory_limit: str | None,
    json_str: str | None,
    json_file: str | None,
) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)

    if compose_inline and compose_inline_file:
        raise click.ClickException(
            "Use either --compose-inline or --compose-inline-file, not both"
        )

    compose_content = compose_inline
    if compose_inline_file:
        compose_content = _load_text_from_path(compose_inline_file)

    payload = _load_json_from_args(json_str, json_file) or {}

    env_payload = _parse_key_value_pairs(env_pairs, option_name="--env")
    exposed_services_payload = _parse_csv_items(exposed_services)

    if isinstance(project_name, str):
        if not project_name.strip():
            raise click.ClickException("--name cannot be empty")
        payload["project_name"] = project_name.strip()
    if isinstance(source_type, str) and source_type:
        payload["source_type"] = source_type
    if isinstance(source_url, str):
        if not source_url.strip():
            raise click.ClickException("--source-url cannot be empty")
        payload["source_url"] = source_url.strip()
    if isinstance(ref, str):
        if not ref.strip():
            raise click.ClickException("--ref cannot be empty")
        payload["ref"] = ref.strip()
    if isinstance(compose_file, str):
        if not compose_file.strip():
            raise click.ClickException("--compose-file cannot be empty")
        payload["compose_file"] = compose_file.strip()
    if compose_content is not None:
        payload["compose_content"] = compose_content
    if env_payload is not None:
        payload["env"] = env_payload
    if pull is not None:
        payload["pull"] = pull
    if build is not None:
        payload["build"] = build
    if isinstance(network_mode, str) and network_mode:
        payload["network_mode"] = network_mode
    if exposed_services is not None:
        payload["exposed_services"] = exposed_services_payload or []
    if isinstance(lifetime_type, str) and lifetime_type:
        payload["lifetime_type"] = lifetime_type
    if time_to_live_seconds is not None:
        payload["time_to_live_seconds"] = time_to_live_seconds
    if cpu_limit is not None:
        payload["cpu_limit"] = cpu_limit
    if isinstance(memory_limit, str):
        if not memory_limit.strip():
            raise click.ClickException("--memory-limit cannot be empty")
        payload["memory_limit"] = memory_limit.strip()

    if not payload:
        raise click.ClickException(
            "Project deploy requires either --json/--json-file payload or explicit compose options"
        )

    project_name_obj = payload.get("project_name")
    if not isinstance(project_name_obj, str) or not project_name_obj.strip():
        raise click.ClickException(
            "Project deploy payload requires non-empty project_name"
        )
    payload["project_name"] = project_name_obj.strip()

    source_type_obj = payload.get("source_type")
    if not isinstance(source_type_obj, str) or source_type_obj not in {
        "git",
        "archive",
        "inline",
    }:
        raise click.ClickException(
            "Project deploy payload requires source_type: git, archive, or inline"
        )

    compose_file_obj = payload.get("compose_file")
    if compose_file_obj is None:
        payload["compose_file"] = "docker-compose.yml"
    elif not isinstance(compose_file_obj, str) or not compose_file_obj.strip():
        raise click.ClickException("compose_file must be a non-empty string")
    else:
        payload["compose_file"] = compose_file_obj.strip()

    if source_type_obj in {"git", "archive"}:
        source_url_obj = payload.get("source_url")
        if not isinstance(source_url_obj, str) or not source_url_obj.strip():
            raise click.ClickException(
                "Git/archive project deploy requires source_url (--source-url/--url)"
            )
        payload["source_url"] = source_url_obj.strip()

    if source_type_obj == "inline":
        compose_content_obj = payload.get("compose_content")
        if not isinstance(compose_content_obj, str) or not compose_content_obj.strip():
            raise click.ClickException(
                "Inline project deploy requires compose_content (--compose-inline or --compose-inline-file)"
            )

    if isinstance(lab_name, str) and lab_name:
        response = _call_control_plane_api(
            cp_state,
            "POST",
            f"/lab/{lab_name}/projects",
            payload=payload,
        )
    else:
        response = _call_control_plane_api(
            cp_state,
            "POST",
            "/projects/scheduled",
            payload=payload,
            query={"scheduling_method": scheduling_method},
        )
    click.echo(json.dumps(response, indent=2))


@control_plane_projects.command("update")
@click.option("--lab", "lab_name", required=True)
@click.option("--name", "project_name", required=True)
@click.option(
    "--network-mode",
    default=None,
    type=click.Choice(
        [
            "internal_private",
            "internal_exposed",
            "external_private",
            "external_exposed",
        ]
    ),
)
@click.option(
    "--json",
    "json_str",
    default=None,
    help="Project payload as JSON object. Required for reliable updates.",
)
@click.option("--json-file", default=None, help="Path to project JSON payload file")
def control_plane_projects_update(
    lab_name: str,
    project_name: str,
    network_mode: str | None,
    json_str: str | None,
    json_file: str | None,
) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)

    payload = _load_json_from_args(json_str, json_file)
    if payload is None:
        raise click.ClickException(
            "Project update requires --json or --json-file with full deploy payload"
        )

    payload["project_name"] = project_name
    if isinstance(network_mode, str) and network_mode:
        payload["network_mode"] = network_mode

    response = _call_control_plane_api(
        cp_state,
        "POST",
        f"/lab/{lab_name}/projects",
        payload=payload,
    )
    click.echo(json.dumps(response, indent=2))


@control_plane_projects.command("delete")
@click.option("--lab", "lab_name", required=True)
@click.option("--name", "project_name", required=True)
def control_plane_projects_delete(lab_name: str, project_name: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(
        cp_state,
        "DELETE",
        f"/lab/{lab_name}/projects/{project_name}",
    )
    click.echo(json.dumps(response, indent=2))


@control_plane_projects.command("delete-all")
@click.option("--lab", "lab_name", required=True)
def control_plane_projects_delete_all(lab_name: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    response = _call_control_plane_api(
        cp_state,
        "DELETE",
        f"/lab/{lab_name}/projects",
    )
    click.echo(json.dumps(response, indent=2))


@control_plane_projects.command("state")
@click.option("--lab", "lab_name", required=True)
@click.option(
    "--action",
    required=True,
    type=click.Choice(["up", "down", "pull", "start", "stop"]),
)
@click.option("--names", required=True, help="Comma-separated project names")
def control_plane_projects_state(lab_name: str, action: str, names: str) -> None:
    state = InfraState()
    cp_state = _control_plane_state_or_fail(state)
    payload = {"names": [n.strip() for n in names.split(",") if n.strip()]}
    response = _call_control_plane_api(
        cp_state,
        "POST",
        f"/lab/{lab_name}/projects/state/{action}",
        payload=payload,
    )
    click.echo(json.dumps(response, indent=2))
