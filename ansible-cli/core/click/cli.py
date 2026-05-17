from __future__ import annotations

import ipaddress
import json
import os
from typing import Any
from urllib import error, request

import click
import core.ansible.inventory as inv
import core.util.dir as dir
from core.ansible.runner import run_playbook
from core.infra.state import InfraState


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


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


def _register_lab_host(
    control_plane: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    control_plane_host = control_plane.get("host") or control_plane.get("ansible_host")
    control_plane_port = int(control_plane.get("port", 8001))
    control_plane_scheme = str(control_plane.get("scheme", "http"))
    control_plane_api_key = control_plane.get("api_key")

    if not isinstance(control_plane_host, str) or not control_plane_host:
        raise click.ClickException("Control plane host is not configured in state")
    if not isinstance(control_plane_api_key, str) or not control_plane_api_key:
        raise click.ClickException("Control plane API key is not configured in state")

    url = f"{control_plane_scheme}://{control_plane_host}:{control_plane_port}/hosts/register"
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": control_plane_api_key,
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(
            f"Failed to register lab host. HTTP {exc.code}: {detail}"
        ) from exc
    except error.URLError as exc:
        raise click.ClickException(
            f"Failed to reach control plane register endpoint: {exc}"
        ) from exc


@click.group()
def cli() -> None:
    pass


@cli.group()
def infra() -> None:
    """Infrastructure lifecycle commands."""


@infra.command("provision")
@click.option(
    "--type", "infra_type", type=click.Choice(["local", "lan", "cloud"]), required=True
)
@click.option("--terraform-workdir", default=".", show_default=True)
@click.option("--terraform-workspace", default="default", show_default=True)
@click.option("--terraform-var-file", default="", show_default=True)
@click.option("--auto-approve/--no-auto-approve", default=True, show_default=True)
def infra_provision(
    infra_type: str,
    terraform_workdir: str,
    terraform_workspace: str,
    terraform_var_file: str,
    auto_approve: bool,
) -> None:
    state = InfraState()
    state.set_provision_type(infra_type)

    if infra_type == "local":
        i = inv.EvLabInventory()
        i.group_insert("local")
        i.host_insert_update(
            host_name="localhost",
            group_name="local",
            host_vars={"ansible_connection": "local", "ansible_host": "127.0.0.1"},
        )
        click.secho(
            "Local infrastructure provisioned (inventory prepared).", fg="green"
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
    state.set_terraform(
        {
            "workdir": terraform_workdir,
            "workspace": terraform_workspace,
            "var_file": terraform_var_file,
            "output_file": output_file,
            "last_output": tf,
        }
    )
    click.secho("Infrastructure provision complete.", fg="green")


@infra.command("status")
def infra_status() -> None:
    state = InfraState()
    provision_type = state.data.get("provision", {}).get("type")

    if provision_type in {"lan", "cloud"}:
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

    click.echo(json.dumps(state.data, indent=2))


@infra.command("destroy")
@click.option("--terraform-workdir", default=None)
@click.option("--terraform-workspace", default=None)
@click.option("--terraform-var-file", default=None)
@click.option("--auto-approve/--no-auto-approve", default=True, show_default=True)
def infra_destroy(
    terraform_workdir: str | None,
    terraform_workspace: str | None,
    terraform_var_file: str | None,
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

    provision_type = state.data.get("provision", {}).get("type")
    if provision_type == "local":
        click.secho("Local mode destroy: nothing to terraform-destroy.", fg="yellow")
        return

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
    click.secho("Infrastructure destroy complete.", fg="green")


@infra.group("bootstrap")
def infra_bootstrap() -> None:
    """Bootstrap control-plane and lab-host nodes."""


@infra_bootstrap.command("control-plane")
@click.option("--device", required=True, help="Device name in inventory or raw IP")
@click.option(
    "--image", "control_plane_image", default="control-plane:latest", show_default=True
)
@click.option("--api-key", "control_plane_api_key", required=True)
@click.option("--port", "control_plane_port", default=8001, type=int, show_default=True)
@click.option(
    "--db-password",
    "control_plane_db_password",
    default="control_plane",
    show_default=True,
)
@click.option("--rabbitmq-user", default="guest", show_default=True)
@click.option("--rabbitmq-password", default="guest", show_default=True)
def bootstrap_control_plane(
    device: str,
    control_plane_image: str,
    control_plane_api_key: str,
    control_plane_port: int,
    control_plane_db_password: str,
    rabbitmq_user: str,
    rabbitmq_password: str,
) -> None:
    state = InfraState()
    target_host, host_vars = _ensure_inventory_host("control_plane", device)

    ok, msg = run_playbook("host.ensure.docker.yaml", {"target_host": target_host})
    if not ok:
        raise click.ClickException(f"Docker setup failed: {msg}")

    ok, msg = run_playbook(
        "control_plane.deploy.yaml",
        {
            "target_host": target_host,
            "control_plane_image": control_plane_image,
            "control_plane_api_key": control_plane_api_key,
            "control_plane_container_port": control_plane_port,
            "control_plane_host_port": control_plane_port,
            "control_plane_db_password": control_plane_db_password,
            "control_plane_rabbitmq_user": rabbitmq_user,
            "control_plane_rabbitmq_password": rabbitmq_password,
        },
    )
    if not ok:
        raise click.ClickException(f"Control plane deploy failed: {msg}")

    ansible_host = str(host_vars.get("ansible_host") or target_host)
    rabbitmq_url = f"amqp://{rabbitmq_user}:{rabbitmq_password}@{ansible_host}:5672/%2F"

    state.set_control_plane(
        {
            "target_host": target_host,
            "host": ansible_host,
            "ansible_host": ansible_host,
            "scheme": "http",
            "port": control_plane_port,
            "api_key": control_plane_api_key,
            "env": {
                "RABBITMQ_URL": rabbitmq_url,
                "RABBITMQ_EXCHANGE": "lab.events",
                "RABBITMQ_QUEUE": "control_plane.heartbeats",
                "RABBITMQ_ROUTING_KEY_PATTERN": "heartbeat.*",
                "DB_URL": f"postgresql+asyncpg://control_plane:{control_plane_db_password}@control-plane-db:5432/control_plane",
                "DOCKER_INTERFACE_API_KEY": control_plane_api_key,
            },
        }
    )

    click.secho("Control plane bootstrap complete.", fg="green")


@infra_bootstrap.command("lab-host")
@click.option("--device", required=True, help="Device name in inventory or raw IP")
@click.option("--image", "lab_host_image", default="lab-host:latest", show_default=True)
@click.option("--api-key", "lab_host_api_key", required=True)
@click.option("--port", "lab_host_port", default=8000, type=int, show_default=True)
@click.option("--host-id", "lab_host_id", default=None)
@click.option(
    "--register-ip",
    default=None,
    help="IP/address control-plane should use for this lab host",
)
@click.option("--cpu-total", default=0, type=int, show_default=True)
@click.option("--memory-total-mb", default=0, type=int, show_default=True)
def bootstrap_lab_host(
    device: str,
    lab_host_image: str,
    lab_host_api_key: str,
    lab_host_port: int,
    lab_host_id: str | None,
    register_ip: str | None,
    cpu_total: int,
    memory_total_mb: int,
) -> None:
    state = InfraState()
    cp_state = state.data.get("control_plane") if isinstance(state.data, dict) else None
    if not isinstance(cp_state, dict) or not cp_state:
        raise click.ClickException(
            "Control plane state is missing. Bootstrap control-plane first."
        )

    target_host, host_vars = _ensure_inventory_host("lab_hosts", device)

    ok, msg = run_playbook("host.ensure.docker.yaml", {"target_host": target_host})
    if not ok:
        raise click.ClickException(f"Docker setup failed: {msg}")

    rabbitmq_url = (
        cp_state.get("env", {}).get("RABBITMQ_URL")
        if isinstance(cp_state.get("env"), dict)
        else None
    )
    if not isinstance(rabbitmq_url, str) or not rabbitmq_url:
        rabbitmq_url = "amqp://guest:guest@localhost:5672/%2F"

    computed_lab_host_id = lab_host_id or f"lab-host-{target_host}"

    ok, msg = run_playbook(
        "lab_host.deploy.yaml",
        {
            "target_host": target_host,
            "lab_host_image": lab_host_image,
            "lab_host_api_key": lab_host_api_key,
            "lab_host_container_port": lab_host_port,
            "lab_host_host_port": lab_host_port,
            "lab_host_id": computed_lab_host_id,
            "lab_host_rabbitmq_url": rabbitmq_url,
        },
    )
    if not ok:
        raise click.ClickException(f"Lab host deploy failed: {msg}")

    ansible_host = str(host_vars.get("ansible_host") or target_host)
    register_host_value = register_ip or ansible_host

    register_payload = {
        "hostname": computed_lab_host_id,
        "ip_address": register_host_value,
        "port": lab_host_port,
        "scheme": "http",
        "status": "online",
        "cpu_total": cpu_total,
        "memory_total_mb": memory_total_mb,
        "api_key": lab_host_api_key,
    }

    register_response = _register_lab_host(cp_state, register_payload)

    state.upsert_lab_host(
        host_key=computed_lab_host_id,
        values={
            "target_host": target_host,
            "ansible_host": ansible_host,
            "register_ip": register_host_value,
            "port": lab_host_port,
            "api_key": lab_host_api_key,
            "lab_host_id": computed_lab_host_id,
            "register_response": register_response,
            "rabbitmq_url": rabbitmq_url,
        },
    )

    click.secho("Lab host bootstrap complete and registered.", fg="green")
