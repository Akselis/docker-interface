import docker
from fastapi import FastAPI

app = FastAPI()
client = docker.from_env()


@app.post("/containers")
def create_container(image: str, name: str):
    container = client.containers.run(image, name=name, detach=True)
    return {"container": container.attrs}


@app.get("/containers")
def list_containers(extended: bool = False):
    containers = client.containers.list(all=True)
    if extended:
        return [{"container": c.attrs} for c in containers]
    else:
        return [
            {
                "container": {
                    "id": c.id,
                    "name": c.name,
                    "image": c.image.tags[0] if c.image.tags else c.image.short_id,
                    "status": c.status,
                }
            }
            for c in containers
        ]


@app.get("/containers/{container_id}")
def get_container(container_id: str):
    container = client.containers.get(container_id)
    return {"container": container.attrs}


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
