from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from constants.const import (
    DNS_PROVIDER_BASE_URL_ENV_VAR,
    DNS_PROVIDER_FILE_ENV_VAR,
    DNS_PROVIDER_TOKEN_ENV_VAR,
    INGRESS_PROVIDER_BASE_URL_ENV_VAR,
    INGRESS_PROVIDER_FILE_ENV_VAR,
    INGRESS_PROVIDER_TOKEN_ENV_VAR,
    PROVIDER_REQUEST_TIMEOUT_SECONDS,
)


def _read_json_map(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_json_map(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


class DnsProvider(Protocol):
    async def ensure_wildcard(
        self,
        *,
        zone: str,
        wildcard_fqdn: str,
        target: str,
    ) -> None: ...


class IngressProvider(Protocol):
    async def ensure_route(
        self,
        *,
        hostname: str,
        upstream_host: str,
        upstream_port: int,
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    async def delete_route(self, *, hostname: str) -> None: ...


@dataclass
class NoopDnsProvider:
    async def ensure_wildcard(
        self,
        *,
        zone: str,
        wildcard_fqdn: str,
        target: str,
    ) -> None:
        _ = (zone, wildcard_fqdn, target)


@dataclass
class NoopIngressProvider:
    async def ensure_route(
        self,
        *,
        hostname: str,
        upstream_host: str,
        upstream_port: int,
        metadata: dict[str, str] | None = None,
    ) -> None:
        _ = (hostname, upstream_host, upstream_port, metadata)

    async def delete_route(self, *, hostname: str) -> None:
        _ = hostname


@dataclass
class FileDnsProvider:
    records_file: str

    async def ensure_wildcard(
        self,
        *,
        zone: str,
        wildcard_fqdn: str,
        target: str,
    ) -> None:
        records = _read_json_map(self.records_file)
        records[wildcard_fqdn] = {
            "zone": zone,
            "type": "A",
            "target": target,
        }
        _write_json_map(self.records_file, records)


@dataclass
class HttpDnsProvider:
    base_url: str
    token: str | None = None
    timeout_seconds: float = PROVIDER_REQUEST_TIMEOUT_SECONDS

    async def ensure_wildcard(
        self,
        *,
        zone: str,
        wildcard_fqdn: str,
        target: str,
    ) -> None:
        payload = {
            "zone": zone,
            "wildcard_fqdn": wildcard_fqdn,
            "target": target,
        }
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/dns/wildcards",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()


@dataclass
class FileIngressProvider:
    routes_file: str

    async def ensure_route(
        self,
        *,
        hostname: str,
        upstream_host: str,
        upstream_port: int,
        metadata: dict[str, str] | None = None,
    ) -> None:
        routes = _read_json_map(self.routes_file)
        routes[hostname] = {
            "upstream_host": upstream_host,
            "upstream_port": upstream_port,
            "metadata": metadata or {},
        }
        _write_json_map(self.routes_file, routes)

    async def delete_route(self, *, hostname: str) -> None:
        routes = _read_json_map(self.routes_file)
        routes.pop(hostname, None)
        _write_json_map(self.routes_file, routes)


@dataclass
class HttpIngressProvider:
    base_url: str
    token: str | None = None
    timeout_seconds: float = PROVIDER_REQUEST_TIMEOUT_SECONDS

    async def ensure_route(
        self,
        *,
        hostname: str,
        upstream_host: str,
        upstream_port: int,
        metadata: dict[str, str] | None = None,
    ) -> None:
        payload = {
            "hostname": hostname,
            "upstream_host": upstream_host,
            "upstream_port": upstream_port,
            "metadata": metadata or {},
        }
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.put(
                f"{self.base_url.rstrip('/')}/ingress/routes/{hostname}",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

    async def delete_route(self, *, hostname: str) -> None:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.delete(
                f"{self.base_url.rstrip('/')}/ingress/routes/{hostname}",
                headers=headers,
            )
            if response.status_code not in {200, 202, 204, 404}:
                response.raise_for_status()


_dns_provider_singleton: DnsProvider | None = None
_ingress_provider_singleton: IngressProvider | None = None


def get_dns_provider() -> DnsProvider:
    global _dns_provider_singleton
    if _dns_provider_singleton is not None:
        return _dns_provider_singleton

    base_url = os.getenv(DNS_PROVIDER_BASE_URL_ENV_VAR)
    token = os.getenv(DNS_PROVIDER_TOKEN_ENV_VAR)
    records_file = os.getenv(DNS_PROVIDER_FILE_ENV_VAR)
    if base_url:
        _dns_provider_singleton = HttpDnsProvider(base_url=base_url, token=token)
    elif records_file:
        _dns_provider_singleton = FileDnsProvider(records_file=records_file)
    else:
        _dns_provider_singleton = NoopDnsProvider()

    return _dns_provider_singleton


def get_ingress_provider() -> IngressProvider:
    global _ingress_provider_singleton
    if _ingress_provider_singleton is not None:
        return _ingress_provider_singleton

    base_url = os.getenv(INGRESS_PROVIDER_BASE_URL_ENV_VAR)
    token = os.getenv(INGRESS_PROVIDER_TOKEN_ENV_VAR)
    routes_file = os.getenv(INGRESS_PROVIDER_FILE_ENV_VAR)
    if base_url:
        _ingress_provider_singleton = HttpIngressProvider(
            base_url=base_url, token=token
        )
    elif routes_file:
        _ingress_provider_singleton = FileIngressProvider(routes_file=routes_file)
    else:
        _ingress_provider_singleton = NoopIngressProvider()

    return _ingress_provider_singleton
