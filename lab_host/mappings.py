from typing import NotRequired, TypedDict

from docker.models.containers import Container
from docker.models.networks import Network
from docker.types import Mount
from models import ContainerArgs, ContainerStorageMount, PortMapping


class ContainerRunKwargs(TypedDict):
    image: str
    name: str
    network: NotRequired[str]
    network_mode: NotRequired[str]
    command: NotRequired[str | None]
    environment: NotRequired[dict[str, str]]
    ports: NotRequired[dict[str, int]]
    cpu_quota: NotRequired[int]
    mem_limit: NotRequired[str]
    user: NotRequired[str]
    read_only: NotRequired[bool]
    privileged: NotRequired[bool]
    cap_drop: NotRequired[list[str]]
    cap_add: NotRequired[list[str]]
    devices: NotRequired[list[str]]
    security_opt: NotRequired[list[str]]
    labels: NotRequired[dict[str, str]]
    mounts: NotRequired[list[Mount]]


def build_ports(ports: list[PortMapping] | None) -> dict[str, int] | None:
    if not ports:
        return None

    port_dict: dict[str, int] = {}

    for p in ports:
        port_key = f"{p.container}/{p.protocol}"
        port_dict[port_key] = p.host

    return port_dict


def build_security_opts(
    seccomp_profile: str | None, apparmor_profile: str | None
) -> list[str] | None:
    opts: list[str] = []

    if seccomp_profile is not None and seccomp_profile != "default":
        opts.append(f"seccomp={seccomp_profile}")

    if apparmor_profile is not None:
        opts.append(f"apparmor={apparmor_profile}")

    return opts or None


def build_mounts(storage: list[ContainerStorageMount] | None) -> list[Mount] | None:
    if not storage:
        return None

    return [
        Mount(
            type=m.type.value,
            source=m.source,
            target=m.target,
            read_only=m.read_only_mount,
        )
        for m in storage
    ]


def summarize_container(container: Container, full: bool = False) -> dict[str, object]:
    attrs_obj = container.attrs
    attrs = attrs_obj if isinstance(attrs_obj, dict) else {}

    config_obj = attrs.get("Config")
    config = config_obj if isinstance(config_obj, dict) else {}

    host_config_obj = attrs.get("HostConfig")
    host_config = host_config_obj if isinstance(host_config_obj, dict) else {}

    network_settings_obj = attrs.get("NetworkSettings")
    network_settings = (
        network_settings_obj if isinstance(network_settings_obj, dict) else {}
    )

    networks_map_obj = network_settings.get("Networks")
    networks_map = networks_map_obj if isinstance(networks_map_obj, dict) else {}

    labels_obj = config.get("Labels")
    labels: dict[str, str] = {}
    if isinstance(labels_obj, dict):
        for key, value in labels_obj.items():
            if isinstance(key, str) and isinstance(value, str):
                labels[key] = value

    image_ref = (
        container.image.tags[0]
        if container.image.tags
        else str(container.image.short_id)
    )

    basic: dict[str, object] = {
        "id": str(container.id),
        "name": str(container.name),
        "image": str(image_ref),
        "status": str(container.status or "unknown"),
        "labels": labels,
        "networks": [str(name) for name in networks_map.keys()],
    }

    if not full:
        return basic

    env_entries_obj = config.get("Env")
    env_entries = env_entries_obj if isinstance(env_entries_obj, list) else []
    env_dict: dict[str, str] = {}
    for item in env_entries:
        if isinstance(item, str) and "=" in item:
            k, v = item.split("=", 1)
            env_dict[k] = v

    cmd_obj = config.get("Cmd")
    command: str | None
    if isinstance(cmd_obj, list):
        command = " ".join([part for part in cmd_obj if isinstance(part, str)])
    elif isinstance(cmd_obj, str):
        command = cmd_obj
    else:
        command = None

    port_bindings_obj = host_config.get("PortBindings")
    port_bindings = port_bindings_obj if isinstance(port_bindings_obj, dict) else {}
    ports: list[dict[str, object]] = []
    for container_port, bindings in port_bindings.items():
        if not isinstance(container_port, str) or "/" not in container_port:
            continue
        container_part, protocol = container_port.split("/", 1)
        if not container_part.isdigit() or not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            host_port = binding.get("HostPort")
            if not isinstance(host_port, str) or not host_port.isdigit():
                continue
            ports.append(
                {
                    "host": int(host_port),
                    "container": int(container_part),
                    "protocol": protocol,
                }
            )

    mounts_obj = attrs.get("Mounts")
    mounts = mounts_obj if isinstance(mounts_obj, list) else []
    storage: list[dict[str, object]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        source = mount.get("Source")
        target = mount.get("Destination")
        mount_type = mount.get("Type")
        rw = mount.get("RW")
        if (
            isinstance(source, str)
            and isinstance(target, str)
            and isinstance(mount_type, str)
        ):
            storage.append(
                {
                    "source": source,
                    "target": target,
                    "type": mount_type,
                    "read_only_mount": not bool(rw if isinstance(rw, bool) else True),
                }
            )

    security_opt_obj = host_config.get("SecurityOpt")
    security_opt = security_opt_obj if isinstance(security_opt_obj, list) else []
    seccomp_profile: str | None = None
    apparmor_profile: str | None = None
    for opt in security_opt:
        if isinstance(opt, str) and opt.startswith("seccomp="):
            seccomp_profile = opt.split("=", 1)[1]
        if isinstance(opt, str) and opt.startswith("apparmor="):
            apparmor_profile = opt.split("=", 1)[1]

    networks: list[dict[str, object]] = []
    for name, data in networks_map.items():
        aliases: list[str] | None = None
        if isinstance(data, dict):
            aliases_obj = data.get("Aliases")
            if isinstance(aliases_obj, list):
                aliases = [a for a in aliases_obj if isinstance(a, str)]
        networks.append(
            {
                "name": str(name),
                "external": None,
                "driver": None,
                "aliases": aliases,
            }
        )

    cpu_quota_obj = host_config.get("CpuQuota")
    memory_obj = host_config.get("Memory")
    pids_obj = host_config.get("PidsLimit")

    restart_policy_obj = host_config.get("RestartPolicy")
    restart_policy = restart_policy_obj if isinstance(restart_policy_obj, dict) else {}

    devices_obj = host_config.get("Devices")
    devices = devices_obj if isinstance(devices_obj, list) else []
    device_paths: list[str] = []
    for device in devices:
        if isinstance(device, dict):
            path = device.get("PathOnHost")
            if isinstance(path, str):
                device_paths.append(path)

    full_payload: dict[str, object] = {
        "image": str(config.get("Image") or image_ref),
        "name": str(container.name),
        "command": command,
        "env": env_dict or None,
        "ports": ports or None,
        "resources": {
            "cpu_count": (
                int(cpu_quota_obj / 100000)
                if isinstance(cpu_quota_obj, int) and cpu_quota_obj > 0
                else None
            ),
            "memory_limit": (
                str(memory_obj)
                if isinstance(memory_obj, int) and memory_obj > 0
                else None
            ),
            "process_limit": (
                pids_obj if isinstance(pids_obj, int) and pids_obj != -1 else None
            ),
        },
        "security": {
            "user": config.get("User") if isinstance(config.get("User"), str) else None,
            "read_only_root_fs": (
                host_config.get("ReadonlyRootfs")
                if isinstance(host_config.get("ReadonlyRootfs"), bool)
                else None
            ),
            "no_new_privileges": None,
            "capabilities_drop": (
                host_config.get("CapDrop")
                if isinstance(host_config.get("CapDrop"), list)
                else None
            ),
            "capabilities_add": (
                host_config.get("CapAdd")
                if isinstance(host_config.get("CapAdd"), list)
                else None
            ),
            "privileged": (
                host_config.get("Privileged")
                if isinstance(host_config.get("Privileged"), bool)
                else None
            ),
            "devices": device_paths or None,
            "seccomp_profile": seccomp_profile,
            "apparmor_profile": apparmor_profile,
        },
        "network": networks or None,
        "storage": storage or None,
        "labels": labels or None,
        "restart_policy": {
            "name": (
                restart_policy.get("Name")
                if isinstance(restart_policy.get("Name"), str)
                else None
            ),
            "retries": (
                restart_policy.get("MaximumRetryCount")
                if isinstance(restart_policy.get("MaximumRetryCount"), int)
                else None
            ),
            "delay": None,
        },
    }

    return full_payload


def summarize_network(network: Network, full: bool = False) -> dict[str, object]:
    attrs_obj = network.attrs
    attrs = attrs_obj if isinstance(attrs_obj, dict) else {}

    labels_obj = attrs.get("Labels")
    labels: dict[str, str] = {}
    if isinstance(labels_obj, dict):
        for k, v in labels_obj.items():
            if isinstance(k, str) and isinstance(v, str):
                labels[k] = v

    containers_obj = attrs.get("Containers")
    container_ids: list[str] = []
    if isinstance(containers_obj, dict):
        container_ids = [str(cid) for cid in containers_obj.keys()]

    summary: dict[str, object] = {
        "id": str(attrs.get("Id") or network.id),
        "name": str(attrs.get("Name") or network.name),
        "driver": str(attrs.get("Driver") or "unknown"),
        "scope": str(attrs.get("Scope") or "unknown"),
        "internal": bool(attrs.get("Internal") is True),
        "labels": labels,
        "containers": container_ids,
    }

    if not full:
        return summary

    summary["attrs"] = attrs
    return summary


def build_container_kwargs(args: ContainerArgs) -> ContainerRunKwargs:
    kwargs: ContainerRunKwargs = {
        # ---------- identity ----------
        "image": args.image,
        "name": args.name,
    }

    if args.network:
        first_network_name = args.network[0].name
        if first_network_name == "none":
            kwargs["network_mode"] = "none"
        else:
            kwargs["network"] = first_network_name

    if args.command is not None:
        kwargs["command"] = args.command

    if args.env is not None:
        kwargs["environment"] = args.env

    ports = build_ports(args.ports)
    if ports is not None:
        kwargs["ports"] = ports

    if args.resources is not None:
        if args.resources.cpu_count is not None:
            kwargs["cpu_quota"] = args.resources.cpu_count * 100000
        if args.resources.memory_limit is not None:
            kwargs["mem_limit"] = args.resources.memory_limit

    if args.security is not None:
        if args.security.user is not None:
            kwargs["user"] = args.security.user
        if args.security.read_only_root_fs is not None:
            kwargs["read_only"] = args.security.read_only_root_fs
        if args.security.privileged is not None:
            kwargs["privileged"] = args.security.privileged
        if args.security.capabilities_drop is not None:
            kwargs["cap_drop"] = args.security.capabilities_drop
        if args.security.capabilities_add is not None:
            kwargs["cap_add"] = args.security.capabilities_add
        if args.security.devices is not None:
            kwargs["devices"] = args.security.devices

        security_opt = build_security_opts(
            seccomp_profile=args.security.seccomp_profile,
            apparmor_profile=args.security.apparmor_profile,
        )
        if security_opt is not None:
            kwargs["security_opt"] = security_opt

    if args.labels is not None:
        kwargs["labels"] = args.labels

    mounts = build_mounts(args.storage)
    if mounts is not None:
        kwargs["mounts"] = mounts

    return kwargs
