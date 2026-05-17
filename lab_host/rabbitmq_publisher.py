# pyright: reportMissingImports=false
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import UTC, datetime
from typing import Any

import docker
import pika

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F")
RABBITMQ_EXCHANGE = os.getenv("RABBITMQ_EXCHANGE", "lab.events")
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "10"))
LAB_HOST_ID = os.getenv("LAB_HOST_ID", socket.gethostname())


def _snapshot_running_containers(client: docker.DockerClient) -> list[dict[str, Any]]:
    containers = client.containers.list(all=True)
    payload: list[dict[str, Any]] = []
    for c in containers:
        c.reload()
        attrs = c.attrs if isinstance(c.attrs, dict) else {}
        config = attrs.get("Config") if isinstance(attrs.get("Config"), dict) else {}
        labels_obj = config.get("Labels")
        labels = labels_obj if isinstance(labels_obj, dict) else {}

        network_settings = (
            attrs.get("NetworkSettings")
            if isinstance(attrs.get("NetworkSettings"), dict)
            else {}
        )
        networks_map = (
            network_settings.get("Networks")
            if isinstance(network_settings.get("Networks"), dict)
            else {}
        )

        image_ref = c.image.tags[0] if c.image.tags else str(c.image.short_id)
        payload.append(
            {
                "id": str(c.id),
                "name": str(c.name),
                "image": str(image_ref),
                "status": str(c.status or "unknown"),
                "labels": {str(k): str(v) for k, v in labels.items()},
                "networks": [str(name) for name in networks_map.keys()],
            }
        )
    return payload


def _publish_loop() -> None:
    client = docker.from_env()

    while True:
        connection = None
        try:
            params = pika.URLParameters(RABBITMQ_URL)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.exchange_declare(
                exchange=RABBITMQ_EXCHANGE,
                exchange_type="topic",
                durable=True,
            )

            while True:
                message = {
                    "host_id": LAB_HOST_ID,
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "containers": _snapshot_running_containers(client),
                }
                routing_key = f"heartbeat.{LAB_HOST_ID}"
                channel.basic_publish(
                    exchange=RABBITMQ_EXCHANGE,
                    routing_key=routing_key,
                    body=json.dumps(message).encode("utf-8"),
                    properties=pika.BasicProperties(delivery_mode=2),
                )
                time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        except Exception:
            time.sleep(3)
        finally:
            if connection and connection.is_open:
                connection.close()


def start_heartbeat_publisher_thread() -> threading.Thread:
    thread = threading.Thread(
        target=_publish_loop,
        name="lab-host-heartbeat-publisher",
        daemon=True,
    )
    thread.start()
    return thread
