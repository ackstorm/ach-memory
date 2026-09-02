"""Explicit, disposable-only Hindsight boundary for the bake-off."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .contracts import BankSnapshot, RetainReceipt, SnapshotObject


class BakeoffRefused(RuntimeError):
    """The requested operation is outside the experiment safety boundary."""


@dataclass(frozen=True)
class BakeoffConfig:
    base_url: str
    api_token: str | None
    tenant: str
    run_id: uuid.UUID
    request_timeout_seconds: float = 30.0
    operation_timeout_seconds: float = 180.0

    @classmethod
    def from_env(cls, env, *, run_id: uuid.UUID | None = None) -> BakeoffConfig:
        if env.get("HINDSIGHT_BAKEOFF_CONFIRM") != "disposable-banks-only":
            raise BakeoffRefused("HINDSIGHT_BAKEOFF_CONFIRM must authorize disposable banks")
        base_url = env.get("HINDSIGHT_BAKEOFF_URL", "http://127.0.0.1:8888").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BakeoffRefused("invalid Hindsight bake-off URL")
        host = parsed.hostname.casefold().strip("[]")
        allowed = {"localhost", "127.0.0.1", "::1"}
        extra = {item.strip().casefold() for item in env.get("HINDSIGHT_BAKEOFF_ALLOWLIST", "").split(",") if item.strip()}
        if host not in allowed and host not in extra:
            raise BakeoffRefused("production URL is not an allowed isolated endpoint")
        production = env.get("MEMORY_HINDSIGHT_URL")
        if production and _normalize_url(production) == _normalize_url(base_url) and host not in allowed:
            raise BakeoffRefused("production URL cannot be used for bake-off")
        return cls(
            base_url=base_url,
            api_token=env.get("HINDSIGHT_BAKEOFF_TOKEN"),
            tenant=env.get("HINDSIGHT_BAKEOFF_TENANT", "default"),
            run_id=run_id or uuid.uuid4(),
        )


def _normalize_url(value: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    return f"{parsed.scheme.casefold()}://{(parsed.hostname or '').casefold()}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"


def bank_id(run_id: uuid.UUID, purpose: str, ordinal: int) -> str:
    safe = re.sub(r"[^a-z0-9-]", "-", purpose.casefold()).strip("-")
    return f"mq55-{run_id.hex[:12]}-{safe}-{ordinal:03d}"


class DisposableHindsight:
    def __init__(self, config: BakeoffConfig, *, artifact_root: Path = Path(".artifacts/memory-quality")):
        self.config = config
        self.artifact_dir = artifact_root / str(config.run_id)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(timeout=config.request_timeout_seconds)

    def _headers(self) -> dict[str, str]:
        headers = {"X-Tenant": self.config.tenant}
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        return headers

    def _registry_path(self) -> Path:
        return self.artifact_dir / "banks.json"

    def _registry(self) -> list[str]:
        path = self._registry_path()
        if not path.exists():
            return []
        value = json.loads(path.read_text())
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise BakeoffRefused("bank registry is malformed")
        return value

    def _record_bank(self, value: str) -> None:
        values = self._registry()
        if value not in values:
            values.append(value)
            tmp = self._registry_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(values, sort_keys=True, separators=(",", ":")))
            os.replace(tmp, self._registry_path())

    def create_bank(self, purpose: str, ordinal: int = 1) -> str:
        value = bank_id(self.config.run_id, purpose, ordinal)
        response = self._client.put(f"{self.config.base_url}/v1/default/banks/{value}", headers=self._headers(), json={})
        response.raise_for_status()
        self._record_bank(value)
        return value

    def import_template(self, bank_id_value: str, template: dict) -> None:
        self._check_bank(bank_id_value)
        response = self._client.post(f"{self.config.base_url}/v1/default/banks/{bank_id_value}/import", headers=self._headers(), json=template)
        response.raise_for_status()

    def retain_and_wait(self, bank_id_value: str, content: str, *, document_id: str, operation_id: str | None = None) -> RetainReceipt:
        self._check_bank(bank_id_value)
        operation_id = operation_id or str(uuid.uuid5(self.config.run_id, f"{bank_id_value}:{document_id}"))
        payload = {"items": [{"content": content, "document_id": document_id, "context": "memory-quality", "strategy": "conversation", "tags": ["source:chat"]}], "async": True, "operation_id": operation_id}
        response = self._client.post(f"{self.config.base_url}/v1/default/banks/{bank_id_value}/memories", headers=self._headers(), json=payload)
        response.raise_for_status()
        record = response.json() if response.content else {}
        operation_id = record.get("operation_id") or operation_id
        started = time.monotonic()
        deadline = started + self.config.operation_timeout_seconds
        state = "pending"
        while time.monotonic() < deadline:
            status = self._client.get(f"{self.config.base_url}/v1/default/banks/{bank_id_value}/operations/{operation_id}", headers=self._headers())
            if status.status_code >= 500:
                # A transient API/DB outage must not turn a committed async
                # retain into a false failure.  Keep polling the same
                # operation ID; persistent outages still end in timeout.
                time.sleep(0.25)
                continue
            status.raise_for_status()
            state = str(status.json().get("status", "pending")).casefold()
            if state in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        if state not in {"completed", "failed"}:
            raise TimeoutError("Hindsight operation did not reach a terminal state")
        return RetainReceipt(document_id=document_id, operation_id=operation_id, terminal_state=state, duration_ms=int((time.monotonic() - started) * 1000))

    def list_bank_objects(self, bank_id_value: str) -> BankSnapshot:
        self._check_bank(bank_id_value)
        documents = self._client.get(f"{self.config.base_url}/v1/default/banks/{bank_id_value}/documents", headers=self._headers())
        memories = self._client.get(f"{self.config.base_url}/v1/default/banks/{bank_id_value}/memories/list", headers=self._headers())
        documents.raise_for_status()
        memories.raise_for_status()
        body = {"documents": documents.json(), "memories": memories.json()}
        objects = []
        for layer, source in (("document", body["documents"].get("items", [])), ("memory", body["memories"].get("items", []))):
            for item in source:
                if isinstance(item, dict):
                    objects.append(SnapshotObject(layer=layer, object_id=str(item.get("id", item.get("document_id", ""))), text=str(item.get("text", item.get("content", ""))), source_ids=tuple(item.get("source_ids", ()))) )
        return BankSnapshot(bank_id=bank_id_value, objects=tuple(objects))

    def dry_run_extract(self, bank_id_value: str, content: str, **options) -> dict:
        self._check_bank(bank_id_value)
        payload = {"content": content, **options}
        url = f"{self.config.base_url}/v1/default/banks/{bank_id_value}/memories/dry-run-extract"
        for attempt in range(2):
            response = self._client.post(url, headers=self._headers(), json=payload)
            if response.status_code < 500 or attempt:
                response.raise_for_status()
                return response.json()
            time.sleep(0.25)
        raise AssertionError("unreachable")

    def reflect(self, bank_id_value: str, query: str, **options) -> dict:
        self._check_bank(bank_id_value)
        response = self._client.post(f"{self.config.base_url}/v1/default/banks/{bank_id_value}/reflect", headers=self._headers(), json={"query": query, **options})
        response.raise_for_status()
        return response.json()

    def search_pages(self, bank_id_value: str, query: str) -> list[dict]:
        self._check_bank(bank_id_value)
        response = self._client.get(f"{self.config.base_url}/v1/default/banks/{bank_id_value}/knowledge-base/search", headers=self._headers(), params={"q": query})
        response.raise_for_status()
        body = response.json()
        return body.get("items", body.get("results", []))

    def read_page(self, bank_id_value: str, page_id: str) -> dict:
        self._check_bank(bank_id_value)
        response = self._client.get(f"{self.config.base_url}/v1/default/banks/{bank_id_value}/knowledge-base/pages/{page_id}", headers=self._headers())
        response.raise_for_status()
        return response.json()

    def create_page(self, bank_id_value: str, name: str, source_query: str) -> str:
        self._check_bank(bank_id_value)
        response = self._client.post(f"{self.config.base_url}/v1/default/banks/{bank_id_value}/knowledge-base/pages", headers=self._headers(), json={"name": name, "source_query": source_query})
        response.raise_for_status()
        body = response.json()
        return str(body.get("id", body.get("page_id", "")))

    def cleanup(self) -> None:
        values = self._registry()
        prefix = f"mq55-{self.config.run_id.hex[:12]}-"
        if any(not value.startswith(prefix) for value in values):
            raise BakeoffRefused("bank registry contains a foreign bank ID")
        for value in values:
            response = self._client.delete(f"{self.config.base_url}/v1/default/banks/{value}", headers=self._headers())
            response.raise_for_status()

    def _check_bank(self, value: str) -> None:
        if not value.startswith(f"mq55-{self.config.run_id.hex[:12]}-"):
            raise BakeoffRefused("bank ID is outside this run")
