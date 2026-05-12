from fastapi import FastAPI
from rabbitmq_consumer import start_consumer_thread

app = FastAPI()


@app.on_event("startup")
def startup_event() -> None:
    start_consumer_thread()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
