from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

import httpx
from constants.const import (
    VAULT_ADDR_ENV_VAR,
    VAULT_KV_MOUNT_DEFAULT,
    VAULT_KV_MOUNT_ENV_VAR,
    VAULT_TOKEN_ENV_VAR,
)


class SecretStore(Protocol):
    async def put_secret(self, secret_path: str, value: str) -> None: ...

    async def get_secret(self, secret_path: str) -> str: ...


class InMemorySecretStore:
    def __init__(self) -> None:
        self._storage: dict[str, str] = {}

    async def put_secret(self, secret_path: str, value: str) -> None:
        self._storage[secret_path] = value

    async def get_secret(self, secret_path: str) -> str:
        try:
            return self._storage[secret_path]
        except KeyError as exc:
            raise KeyError(f"Secret not found at path: {secret_path}") from exc


class VaultKvV2SecretStore:
    def __init__(
        self,
        *,
        addr: str,
        token: str,
        mount: str = VAULT_KV_MOUNT_DEFAULT,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.addr = addr.rstrip("/")
        self.token = token
        self.mount = mount.strip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def _headers(self) -> Mapping[str, str]:
        return {
            "X-Vault-Token": self.token,
            "Content-Type": "application/json",
        }

    def _data_url(self, secret_path: str) -> str:
        clean_path = secret_path.lstrip("/")
        return f"{self.addr}/v1/{self.mount}/data/{clean_path}"

    async def put_secret(self, secret_path: str, value: str) -> None:
        payload = {"data": {"value": value}}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self._data_url(secret_path),
                headers=self._headers,
                json=payload,
            )
            response.raise_for_status()

    async def get_secret(self, secret_path: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                self._data_url(secret_path),
                headers=self._headers,
            )
            response.raise_for_status()

        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        inner = data.get("data") if isinstance(data, dict) else None
        value = inner.get("value") if isinstance(inner, dict) else None
        if not isinstance(value, str):
            raise ValueError(
                f"Vault secret at path '{secret_path}' is missing string value"
            )
        return value


def build_host_api_key_secret_path(hostname: str) -> str:
    safe_name = "".join(ch for ch in hostname if ch.isalnum() or ch in "-_")
    if not safe_name:
        raise ValueError("hostname must contain at least one alphanumeric character")
    return f"lab-hosts/{safe_name}/api-key"


_secret_store_singleton: SecretStore | None = None


def get_secret_store() -> SecretStore:
    global _secret_store_singleton

    if _secret_store_singleton is not None:
        return _secret_store_singleton

    vault_addr = os.getenv(VAULT_ADDR_ENV_VAR)
    vault_token = os.getenv(VAULT_TOKEN_ENV_VAR)
    vault_mount = os.getenv(VAULT_KV_MOUNT_ENV_VAR, VAULT_KV_MOUNT_DEFAULT)

    if vault_addr and vault_token:
        _secret_store_singleton = VaultKvV2SecretStore(
            addr=vault_addr,
            token=vault_token,
            mount=vault_mount,
        )
    else:
        _secret_store_singleton = InMemorySecretStore()

    return _secret_store_singleton
