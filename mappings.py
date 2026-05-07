from typing import NotRequired, TypedDict

from docker.types import Mount

from models import ContainerArgs, ContainerStorageMount, PortMapping


class ContainerRunKwargs(TypedDict):
    image: str
    name: str
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


def build_container_kwargs(args: ContainerArgs) -> ContainerRunKwargs:
    kwargs: ContainerRunKwargs = {
        # ---------- identity ----------
        "image": args.image,
        "name": args.name,
    }

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
