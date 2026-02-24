from __future__ import annotations

"""Minimal Kalshi REST client.

We intentionally keep dependencies at stdlib (urllib) so the pipeline stays lightweight.

Base URLs (observed in existing scripts):
- prod: https://api.elections.kalshi.com
- demo: https://demo-api.kalshi.co

Auth: RSA-PSS SHA256 headers via kalshi_auth.rest_auth_headers().
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..kalshi_auth import KalshiKey, rest_auth_headers


@dataclass(frozen=True)
class KalshiRestConfig:
    env: str = "demo"  # demo|prod
    timeout_sec: float = 50.0

    @property
    def base_url(self) -> str:
        if self.env == "demo":
            return "https://demo-api.kalshi.co"
        if self.env == "prod":
            return "https://api.elections.kalshi.com"
        raise ValueError(f"Unknown env: {self.env}")


class KalshiRestError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Kalshi REST HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def _req_json(
    *,
    cfg: KalshiRestConfig,
    key: Optional[KalshiKey],
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = cfg.base_url + path
    if params:
        url += "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})

    headers: Dict[str, str] = {"Accept": "application/json"}
    data: Optional[bytes] = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")

    if key is not None:
        headers.update(rest_auth_headers(key, method=method, path=path))

    req = urllib.request.Request(url, method=method.upper(), headers=headers, data=data)

    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise KalshiRestError(e.code, raw)


def get_json(*, cfg: KalshiRestConfig, key: Optional[KalshiKey], path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _req_json(cfg=cfg, key=key, method="GET", path=path, params=params)


def post_json(*, cfg: KalshiRestConfig, key: Optional[KalshiKey], path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return _req_json(cfg=cfg, key=key, method="POST", path=path, body=body)


def delete_json(*, cfg: KalshiRestConfig, key: Optional[KalshiKey], path: str) -> Dict[str, Any]:
    return _req_json(cfg=cfg, key=key, method="DELETE", path=path)
