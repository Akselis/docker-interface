from fastapi import FastAPI

import models as m

app = FastAPI()


@app.get("/containers/start")
async def start_container():
    return {"message": "Hello World"}


@app.post("/items/")
async def create_container(container: m.Container):
    return container
