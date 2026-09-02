"""Pinned upstream identity and a narrow subprocess bridge."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

HASHES = {
    "src/core/missions.ts": "556859a337e7bd5eb48b9ec3c16caf99ba9537f0c4a22b44745c4bd570c763fa",
    "src/core/transcript.ts": "b0dee61adedbcceba12b80acf37ecec456f06dff7481183867d14080c0488eda",
    "src/core/transcript-codex.ts": "afb8163db21b9e85ebd8c297f6cfd6da4607557c14c85c113608672278a3f4fe",
    "src/core/chat.ts": "e2e64fc6e22e0383643de55ebfb1d275a563798f2864273febb5f8220a6e361a",
    "src/core/retain-hook.ts": "b6590ef8582ae650b42db21004973912e9ac287d8f55ebbd4d49f604b5be496d",
    "src/core/hindsight.ts": "cd3d32d2586ad3d6ce86f0764fe2bd5db7bed1b48f25ecce6d6ce2987a5eb3ec",
    "src/core/retain-cursor.ts": "bd4883403b0b0425484ffdb00f8d42d65159612cd037cb95c808e066d1de320b",
    "src/core/types.ts": "b5f46397dc6d260ac256c527403c67bef797cb52409c461c040980c6c8940d17",
    "package.json": "c2304026211a71a6ac21d4caa9857fda6d1df7e7cb1060bd5b3053dca196d6b9",
    "package-lock.json": "3cb971b394933410511d11831bed485df457172a897311d914984f196fe1a621",
}


class SourceDrift(RuntimeError):
    pass


@dataclass(frozen=True)
class OfficialSource:
    root: Path
    package_version: Literal["0.5.1"]
    release_commit: Literal["c61c4e7d7"]
    checkout_commit: str
    sha256_by_relpath: Mapping[str, str]


class NormalizedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "tool", "meta"]
    text: str
    source_start: int | None = None
    source_end: int | None = None


class RuntimeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    turn_count: int
    error_code: str | None = None


def verify_official_source(root: Path) -> OfficialSource:
    root = root.resolve()
    if not root.is_dir():
        raise SourceDrift("official source directory is missing")
    for relpath, expected in HASHES.items():
        path = root / relpath
        if not path.is_file() or path.is_symlink():
            raise SourceDrift(f"missing or unsafe upstream file: {relpath}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise SourceDrift(f"hash drift in {relpath}")
    package = json.loads((root / "package.json").read_text())
    if package.get("version") != "0.5.1":
        raise SourceDrift("official package version drift")
    git_root = root.parent.parent
    try:
        checkout = subprocess.run(["git", "-C", str(git_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(git_root), "diff", "--quiet", "c61c4e7d7", "--", "hindsight-integrations/coding-agents/src", "hindsight-integrations/coding-agents/package.json", "hindsight-integrations/coding-agents/package-lock.json"], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SourceDrift("official package subtree is not at the pinned release") from exc
    return OfficialSource(root, "0.5.1", "c61c4e7d7", checkout, HASHES.copy())


class OfficialRuntime:
    def __init__(self, source: OfficialSource):
        self.source = source

    def _invoke(self, payload: dict, env: Mapping[str, str]) -> dict:
        command = ["npm", "exec", "--yes", "--package=tsx@4.20.6", "--", "tsx", str(Path(__file__).with_name("official_bridge.ts"))]
        safe_env = {key: value for key, value in env.items() if key in {"PATH", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"}}
        with tempfile.TemporaryDirectory(prefix="mq55-home-") as home:
            safe_env["HOME"] = home
            result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, timeout=90, check=False, env=safe_env)
        if result.returncode:
            raise RuntimeError("official runtime failed")
        return json.loads(result.stdout)

    def normalize_claude(self, path: Path) -> tuple[NormalizedTurn, ...]:
        body = self._invoke({"op": "normalize-claude", "transcriptPath": str(path), "sourceRoot": str(self.source.root)}, os.environ)
        return tuple(NormalizedTurn.model_validate(item) for item in body.get("turns", []))

    def retain_claude(self, event: dict, env: Mapping[str, str]) -> RuntimeReceipt:
        event = {**event, "runPrefix": event.get("runPrefix", "")}
        body = self._invoke(event, env)
        return RuntimeReceipt.model_validate(body)
