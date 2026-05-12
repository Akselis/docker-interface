FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY controller.py docker_interface.py mappings.py models.py rabbitmq_publisher.py ./

EXPOSE 8000

CMD ["uvicorn", "controller:app", "--host", "0.0.0.0", "--port", "8000"]
