from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
from constants.const import LAB_HOST_API_KEY_HEADER, LAB_HOST_REQUEST_TIMEOUT_SECONDS


def build_lab_host_base_url(ip_address: str, port: int, scheme: str) -> str:
    clean_scheme = scheme.strip().lower()
    if clean_scheme not in {"http", "https"}:
        raise ValueError("scheme must be either 'http' or 'https'")
    return f"{clean_scheme}://{ip_address}:{port}"


class LabHostClient:
    async def call(
        self,
        *,
        ip_address: str,
        port: int,
        scheme: str,
        api_key: str,
        method: str,
        endpoint_path: str,
        query: Mapping[str, str] | None = None,
        json_body: object | None = None,
        timeout_seconds: float = LAB_HOST_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        base_url = build_lab_host_base_url(ip_address, port, scheme)
        path = endpoint_path if endpoint_path.startswith("/") else f"/{endpoint_path}"
        target_url = f"{base_url}{path}"

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.request(
                method=method.upper(),
                url=target_url,
                headers={LAB_HOST_API_KEY_HEADER: api_key},
                params=dict(query) if query else None,
                json=json_body,
            )

        body: Any
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = response.text

        return {
            "status_code": response.status_code,
            "url": str(response.url),
            "body": body,
        }

    async def check_health(
        self,
        *,
        ip_address: str,
        port: int,
        scheme: str,
        api_key: str,
        timeout_seconds: float = LAB_HOST_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        return await self.call(
            ip_address=ip_address,
            port=port,
            scheme=scheme,
            api_key=api_key,
            method="GET",
            endpoint_path="/health",
            timeout_seconds=timeout_seconds,
        )
