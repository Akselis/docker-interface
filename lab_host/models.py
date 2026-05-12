from enum import Enum

from pydantic import BaseModel, field_validator


class NetworkDriver(str, Enum):
    BRIDGE = "bridge"
    OVERLAY = "overlay"
    MACVLAN = "macvlan"
    NONE = "none"


class StorageType(str, Enum):
    BIND = "bind"
    VOLUME = "volume"
    TMPFS = "tmpfs"


class ContainerResourceArgs(BaseModel):
    cpu_count: int | None = None
    memory_limit: str | None = None
    process_limit: int | None = None


class ContainerNetworkArgs(BaseModel):
    name: str
    external: bool | None = None
    driver: NetworkDriver | None = None
    aliases: list[str] | None = None


class ContainerStorageMount(BaseModel):
    source: str
    target: str
    type: StorageType
    read_only_mount: bool


class RestartPolicy(BaseModel):
    name: str | None = None
    retries: int | None = None
    delay: int | None = None


class PortMapping(BaseModel):
    host: int
    container: int
    protocol: str

    @field_validator("host", "container")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("Invalid port range")
        return v


class ContainerSecurityArgs(BaseModel):
    user: str | None = None
    read_only_root_fs: bool | None = None
    no_new_privileges: bool | None = None
    capabilities_drop: list[str] | None = None
    capabilities_add: list[str] | None = None
    privileged: bool | None = None
    devices: list[str] | None = None
    seccomp_profile: str | None = None
    apparmor_profile: str | None = None


class ContainerArgs(BaseModel):
    image: str
    name: str
    command: str | None = None
    env: dict[str, str] | None = None
    ports: list[PortMapping] | None = None
    resources: ContainerResourceArgs | None = None
    security: ContainerSecurityArgs | None = None
    network: list[ContainerNetworkArgs] | None = None
    storage: list[ContainerStorageMount] | None = None
    labels: dict[str, str] | None = None
    restart_policy: RestartPolicy | None = None


class NetworkIpamArgs(BaseModel):
    subnet: str | None = None
    gateway: str | None = None
    ip_range: str | None = None


class NetworkCreateArgs(BaseModel):
    name: str
    driver: NetworkDriver | None = None
    internal: bool | None = None
    attachable: bool | None = None
    labels: dict[str, str] | None = None
    ipam: NetworkIpamArgs | None = None


class NetworkConnectArgs(BaseModel):
    container_id: str
    aliases: list[str] | None = None
    ipv4_address: str | None = None


class NetworkDisconnectArgs(BaseModel):
    container_id: str
    force: bool = False


class ComposeSourceType(str, Enum):
    GIT = "git"
    ARCHIVE = "archive"
    INLINE = "inline"


class ComposeDeployArgs(BaseModel):
    project_name: str
    source_type: ComposeSourceType
    source_url: str | None = None
    ref: str | None = None
    compose_file: str = "docker-compose.yml"
    compose_content: str | None = None
    env: dict[str, str] | None = None
    pull: bool = True
    build: bool = False


class ComposeActionArgs(BaseModel):
    env: dict[str, str] | None = None
    remove_volumes: bool = False
    remove_images: bool = False
    timeout_seconds: int = 120


class ComposeLogsArgs(BaseModel):
    tail: int = 200
    follow: bool = False


class VolumeCreateArgs(BaseModel):
    name: str
    driver: str | None = None
    labels: dict[str, str] | None = None


class ExecCommandRequest(BaseModel):
    command: str | list[str]
    workdir: str | None = None
    user: str | None = None
    environment: dict[str, str] | None = None
    privileged: bool = False
