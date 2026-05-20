from __future__ import annotations

from enum import Enum
from typing import Literal

from db.models.host import HostStatus
from db.models.lab import LabStatus
from db.models.project import ComposeSourceType
from db.models.shared import LifetimeType
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
    cpu_count: int | None = None
    memory_limit: str | None = None
    process_limit: int | None = None

    @field_validator("cpu_count", "process_limit")
    @classmethod
    def validate_positive_ints(_cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("Value must be greater than 0")
        return value


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
    def validate_port(_cls, value: int) -> int:
        if not (1 <= value <= 65535):
            raise ValueError("Invalid port range")
        return value


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


class EnvironmentNetworkMode(str, Enum):
    OFFLINE = "offline"
    INTERNAL_PRIVATE = "internal_private"
    INTERNAL_EXPOSED = "internal_exposed"
    EXTERNAL_PRIVATE = "external_private"
    EXTERNAL_EXPOSED = "external_exposed"


class DeployEnvironmentRequest(BaseModel):
    image: str
    name: str
    network_mode: EnvironmentNetworkMode
    command: str | None = None
    env: dict[str, str] | None = None
    ports: list[PortMapping] | None = None
    resources: ContainerResourceArgs | None = None
    security: ContainerSecurityArgs | None = None
    network: list[ContainerNetworkArgs] | None = None
    storage: list[ContainerStorageMount] | None = None
    labels: dict[str, str] | None = None
    restart_policy: RestartPolicy | None = None

    lifetime_type: LifetimeType = LifetimeType.EPHEMERAL
    time_to_live_seconds: int | None = None

    @field_validator("time_to_live_seconds")
    @classmethod
    def validate_ttl(_cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("time_to_live_seconds must be greater than 0")
        return value


class RegisterHostRequest(BaseModel):
    hostname: str = Field(min_length=1, max_length=100)
    ip_address: str = Field(min_length=1, max_length=255)
    port: int = Field(default=8000, ge=1, le=65535)
    scheme: Literal["http", "https"] = "http"
    status: HostStatus = HostStatus.ONLINE
    cpu_total: int = Field(ge=0)
    memory_total_mb: int = Field(ge=0)
    api_key: str = Field(min_length=1)
    base_domain: str | None = Field(default=None, min_length=1, max_length=255)
    dns_zone: str | None = Field(default=None, min_length=1, max_length=255)
    ingress_target: str | None = Field(default=None, min_length=1, max_length=255)


class CallLabHostRequest(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    endpoint_path: str = Field(min_length=1)
    query: dict[str, str] | None = None
    json_body: object | None = None
    timeout_seconds: float = Field(default=15.0, gt=0, le=120.0)


class CreateLabRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host_id: int = Field(gt=0)
    cpu_limit: int | None = Field(default=None, ge=0)
    memory_limit_mb: int | None = Field(default=None, ge=0)
    status: LabStatus = LabStatus.STOPPED


class CreateScheduledLabRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cpu_limit: int | None = Field(default=None, ge=0)
    memory_limit_mb: int | None = Field(default=None, ge=0)
    status: LabStatus = LabStatus.STOPPED


class NameListRequest(BaseModel):
    names: list[str] = Field(min_length=1)


class ComposeDeployRequest(BaseModel):
    project_name: str
    source_type: ComposeSourceType
    source_url: str | None = None
    ref: str | None = None
    compose_file: str = "docker-compose.yml"
    compose_content: str | None = None
    env: dict[str, str] | None = None
    pull: bool = True
    build: bool = False
    network_mode: EnvironmentNetworkMode = EnvironmentNetworkMode.INTERNAL_PRIVATE
    exposed_services: list[str] | None = None
    lifetime_type: LifetimeType = LifetimeType.PERSISTENT
    time_to_live_seconds: int | None = None
    cpu_limit: float | None = None
    memory_limit: str | None = None

    @field_validator("cpu_limit")
    @classmethod
    def validate_cpu_limit(_cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("cpu_limit must be greater than 0")
        return value

    @field_validator("memory_limit")
    @classmethod
    def validate_memory_limit(_cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("memory_limit cannot be empty")
        return value

    @field_validator("time_to_live_seconds")
    @classmethod
    def validate_ttl(_cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("time_to_live_seconds must be greater than 0")
        return value
