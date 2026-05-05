from enum import Enum

from pydantic import BaseModel, Field, field_validator


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
    cpu_count: int = 1
    memory_limit: str = "512m"
    process_limit: int = 100


class ContainerNetworkArgs(BaseModel):
    name: str
    external: bool = False
    driver: NetworkDriver = NetworkDriver.BRIDGE
    aliases: list[str] = Field(default_factory=list)


class ContainerStorageMount(BaseModel):
    source: str
    target: str
    type: StorageType = StorageType.BIND
    read_only_mount: bool = True


class RestartPolicy(BaseModel):
    name: str = "on-failure"
    retries: int | None = None
    delay: int = 0


class PortMapping(BaseModel):
    host: int
    container: int
    protocol: str = "tcp"

    @field_validator("host", "container")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("Invalid port range")
        return v


class ContainerSecurityArgs(BaseModel):
    user: str = "1000:1000"  # default user
    read_only_root_fs: bool = True  # container's root filesystem cannot be modified
    no_new_privileges: bool = True  # container cannot gain new privileges
    capabilities_drop: list[str] = Field(default_factory=lambda: ["ALL"])
    capabilities_add: list[str] = Field(
        default_factory=list
    )  # capabilities to add to the container
    privileged: bool = False  # root access
    devices: list[str] = Field(
        default_factory=list
    )  # exposes hardware, kernel interfaces
    seccomp_profile: str = "default"
    apparmor_profile: str = "docker-default"


class ContainerArgs(BaseModel):
    image: str
    name: str
    command: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[PortMapping] = Field(default_factory=list)
    resources: ContainerResourceArgs = Field(default_factory=ContainerResourceArgs)
    security: ContainerSecurityArgs = Field(default_factory=ContainerSecurityArgs)
    network: list[ContainerNetworkArgs] = Field(default_factory=list)
    storage: list[ContainerStorageMount] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    restart_policy: RestartPolicy = Field(default_factory=RestartPolicy)
