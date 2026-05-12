# Lab Host API

## Build image

```/dev/null/commands.sh#L1-1
docker build -t lab-host-api .
```

## Run container

The API controls Docker and Docker Compose on the host, so mount the Docker socket.

```/dev/null/commands.sh#L1-11
docker run -d \
  --name lab-host-api \
  -p 8000:8000 \
  -e DOCKER_INTERFACE_API_KEY=replace-with-strong-key \
  -e COMPOSE_PROJECTS_DIR=/tmp/lab_host/compose \
  -e LAB_HOST_ID=lab-host-1 \
  -e RABBITMQ_URL=amqp://guest:guest@control-plane-rabbitmq:5672/%2F \
  -e RABBITMQ_EXCHANGE=lab.events \
  -e HEARTBEAT_INTERVAL_SECONDS=10 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --restart unless-stopped \
  lab-host-api
```

Lab host publishes heartbeat messages with running container states to RabbitMQ on startup and then periodically.

## Compose deploy API

### Deploy compose package from git

```/dev/null/http.json#L1-12
POST /compose/deploy
{
  "project_name": "lab-a",
  "source_type": "git",
  "source_url": "https://github.com/example/repo.git",
  "ref": "main",
  "compose_file": "docker-compose.yml",
  "pull": true,
  "build": false
}
```

### Deploy inline compose content

```/dev/null/http.json#L1-11
POST /compose/deploy
{
  "project_name": "lab-inline",
  "source_type": "inline",
  "compose_file": "docker-compose.yml",
  "compose_content": "services:\n  nginx:\n    image: nginx:latest\n    ports:\n      - \"8080:80\""
}
```

### Lifecycle actions

- `POST /compose/{project_name}/up`
- `POST /compose/{project_name}/down`
- `POST /compose/{project_name}/pull`
- `GET /compose/{project_name}/ps`
- `POST /compose/{project_name}/logs`
- `DELETE /compose/{project_name}`

## Volume management API

- `POST /volumes` - create a named volume
- `GET /volumes` - list all volumes
- `DELETE /volumes/{volume_name}?force=true` - remove one volume
- `POST /volumes/prune` - remove all unused volumes
