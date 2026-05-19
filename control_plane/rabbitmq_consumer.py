# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Any

import pika
from reconciliation import reconcile_heartbeat_payload

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F")
RABBITMQ_EXCHANGE = os.getenv("RABBITMQ_EXCHANGE", "lab.events")
RABBITMQ_QUEUE = os.getenv("RABBITMQ_QUEUE", "control_plane.heartbeats")
RABBITMQ_ROUTING_KEY_PATTERN = os.getenv("RABBITMQ_ROUTING_KEY_PATTERN", "heartbeat.*")
RECONCILE_INTERVAL_SECONDS = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "45"))

logger = logging.getLogger(__name__)


def handle_heartbeat(payload: dict[str, Any]) -> None:
    asyncio.run(reconcile_heartbeat_payload(payload))


def _periodic_reconcile_forever() -> None:
    while True:
        try:
            asyncio.run(reconcile_heartbeat_payload({}))
        except Exception as exc:
            logger.warning("Periodic reconciliation failed: %s", exc)
        finally:
            threading.Event().wait(RECONCILE_INTERVAL_SECONDS)


def _consume_forever() -> None:
    params = pika.URLParameters(RABBITMQ_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.exchange_declare(
        exchange=RABBITMQ_EXCHANGE, exchange_type="topic", durable=True
    )
    channel.queue_declare(queue=RABBITMQ_QUEUE, durable=True)
    channel.queue_bind(
        exchange=RABBITMQ_EXCHANGE,
        queue=RABBITMQ_QUEUE,
        routing_key=RABBITMQ_ROUTING_KEY_PATTERN,
    )

    def _on_message(
        ch: Any,
        method: Any,
        properties: Any,
        body: bytes,
    ) -> None:
        try:
            data = json.loads(body.decode("utf-8"))
            if isinstance(data, dict):
                handle_heartbeat(data)
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as exc:
            logger.warning("Heartbeat processing failed: %s", exc)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    channel.basic_qos(prefetch_count=10)
    channel.basic_consume(queue=RABBITMQ_QUEUE, on_message_callback=_on_message)
    channel.start_consuming()


def start_consumer_thread() -> threading.Thread:
    periodic = threading.Thread(
        target=_periodic_reconcile_forever,
        name="control-plane-periodic-reconcile",
        daemon=True,
    )
    periodic.start()

    thread = threading.Thread(
        target=_consume_forever, name="rabbitmq-heartbeat-consumer", daemon=True
    )
    thread.start()
    return thread
