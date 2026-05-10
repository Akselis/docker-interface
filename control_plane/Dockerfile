FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/control_plane

# This Dockerfile expects build context to be the project root (docker-interface).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/control_plane

EXPOSE 8000

CMD ["uvicorn", "controller:app", "--host", "0.0.0.0", "--port", "8000"]
