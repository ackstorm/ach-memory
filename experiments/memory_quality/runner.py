"""CLI orchestration with refusal-first lifecycle commands."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx

from .contracts import corpus_digest, load_delivery_cases, load_semantic_cases
from .hindsight import BakeoffConfig, BakeoffRefused
from .upstream import verify_official_source

ROOT = Path(__file__).parent


def preflight(env=None) -> dict:
    env = env or os.environ
    config = BakeoffConfig.from_env(env)
    source_root = Path(env.get("HINDSIGHT_CODING_AGENTS_DIR", ROOT.parents[2] / "hindsight/hindsight-integrations/coding-agents"))
    source = verify_official_source(source_root)
    semantic = load_semantic_cases(ROOT / "corpus/semantic.jsonl")
    delivery = load_delivery_cases(ROOT / "corpus/delivery.jsonl")
    with httpx.Client(timeout=config.request_timeout_seconds) as client:
        response = client.get(f"{config.base_url}/openapi.json")
        response.raise_for_status()
        version = response.json().get("info", {}).get("version")
    if version != "0.9.2":
        raise BakeoffRefused("Hindsight API version must be 0.9.2")
    return {"hindsight_version": version, "official_package_version": source.package_version, "official_checkout_commit": source.checkout_commit, "semantic_digest": corpus_digest(semantic), "delivery_digest": corpus_digest(delivery), "mutating_requests": 0, "run_id": str(config.run_id)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m experiments.memory_quality.runner")
    parser.add_argument("command", choices=("preflight", "legacy-calibrate", "run", "score", "cleanup", "report"))
    args = parser.parse_args(argv)
    if args.command == "preflight":
        print(json.dumps(preflight(), sort_keys=True))
        return 0
    if args.command in {"run", "cleanup"}:
        raise SystemExit("live bake-off commands require the measured-run implementation and explicit operator setup")
    raise SystemExit(f"{args.command} requires a completed measured run")


if __name__ == "__main__":
    main()
