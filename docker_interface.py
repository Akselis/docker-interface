import docker
from fastapi import FastAPI

app = FastAPI()
client = docker.from_env()


@app.post("/containers")
def create_container(image: str, name: str = None):
    container = client.containers.run(image, name=name, detach=True)
    return {"id": container.id}


@app.get("/containers")
def list_containers():
    containers = client.containers.list(all=True)
    return [{"id": c.id, "status": c.status} for c in containers]


@app.post("/containers/{container_id}/stop")
def stop_container(container_id: str):
    container = client.containers.get(container_id)
    container.stop()
    return {"status": "stopped"}


@app.delete("/containers/{container_id}")
def remove_container(container_id: str):
    container = client.containers.get(container_id)
    container.remove(force=True)
    return {"status": "removed"}
