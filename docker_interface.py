import docker

from mappings import build_container_kwargs
from models import ContainerArgs

client = docker.from_env()


def create_container(args: ContainerArgs):
    kwargs = build_container_kwargs(args)
    container = client.containers.run(**kwargs)
    return container
