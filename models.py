from pydantic import BaseModel


class ContainerResourceArgs(BaseModel):
    cpu_count: int | None = 1
    memory_limit: str | None = "512m"
    process_limit: int | None = 100


class ContainerNetworkArgs(BaseModel):
    name: str | None = None
    port: int | None = None


class ContainerStorageArgs(BaseModel):
    source: str
    target: str
    readonly: bool | None = True


class ContainerSecurityArgs(BaseModel):
    user: str | None = "1000:1000"  # default user
    readonly: bool | None = True  # container's root filesystem cannot be modified
    no_new_privileges: bool | None = True  # container cannot gain new privileges
    cap_drop: list[str] | None = ["ALL"]  # capabilities to drop from the container
    cap_add: list[str] | None = []  # capabilities to add to the container


class ContainerArgs(BaseModel):
    image: str
    name: str
    command: str | None = None
    env: dict[str, str] | None = None
    resources: ContainerResourceArgs | None = None
    network: ContainerNetworkArgs | None = None
    storage: dict[str, str] | None = None
    labels: dict[str, str] | None = None
    restart_policy: str | None = None


{
    "image": "jupyter/base-notebook",
    "command": null,
    "env": {"USER": "student1"},
    "resources": {"cpu": 1, "memory": "512m"},
    "network": {"name": "lab-net", "port": 8888},
    "storage": {"volume": "/data/student1"},
    "labels": {"user": "student1", "lab": "jupyter"},
}
