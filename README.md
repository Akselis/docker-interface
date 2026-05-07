# Docker Interface API

## Build image

```/dev/null/commands.sh#L1-1
docker build -t docker-interface-api .
```

## Run container

Set your API key and mount the Docker socket so this API can manage containers on the host.

```/dev/null/commands.sh#L1-9
docker run -d \
  --name docker-interface-api \
  -p 8000:8000 \
  -e DOCKER_INTERFACE_API_KEY=replace-with-strong-key \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --restart unless-stopped \
  docker-interface-api
```

## Example request

```/dev/null/commands.sh#L1-5
curl -X GET "http://localhost:8000/containers" \
  -H "X-API-Key: replace-with-strong-key"
```
