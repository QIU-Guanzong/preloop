#!/usr/bin/env python
"""Render or idempotently install the public weekly model-price review flow.

Rendering is local and the default. --apply writes through the authenticated
Preloop API; --enable also arms the Monday schedule. No run is triggered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import yaml

from preloop.models.schemas.flow import FlowCreate

ROOT = Path(__file__).resolve().parents[1]
PRESET = ROOT / "backend/presets/015-weekly-model-price-review.yaml"


def render_flow(
    *,
    ai_model_id: str,
    tracker_id: str,
    project_id: str,
    repository_url: str,
    branch: str = "main",
    enable: bool = False,
) -> dict[str, Any]:
    """Bind an actual model and repository and validate the public preset."""
    model, tracker, project = (
        str(UUID(value)) for value in (ai_model_id, tracker_id, project_id)
    )
    parsed = urlsplit(repository_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Repository must be an HTTPS URL without credentials, query, or fragment"
        )
    if not branch.strip() or branch.startswith("-"):
        raise ValueError("A source branch is required")
    payload = yaml.safe_load(PRESET.read_text())
    payload.pop("slug", None)
    identity = hashlib.sha256(
        f"{tracker}:{project}:{repository_url}".encode()
    ).hexdigest()[:16]
    payload["name"] = f"Weekly model price review ({identity})"
    payload["description"] += f"\nManaged by install_model_price_review.py: {identity}."
    payload.update(ai_model_id=model, is_enabled=enable, is_preset=False)
    clone = payload["git_clone_config"]
    clone["source_branch"] = branch
    clone["repositories"] = [
        {
            "tracker_id": tracker,
            "project_id": project,
            "repository_url": repository_url,
            "clone_path": "workspace",
            "branch": branch,
        }
    ]
    return FlowCreate.model_validate(payload).model_dump(mode="json", exclude_none=True)


def install_flow(client: httpx.Client, payload: dict[str, Any]) -> dict[str, Any]:
    """Create one bound flow or update its prior managed instance."""
    matches = []
    for skip in range(0, 2000, 100):
        response = client.get("flows", params={"skip": skip, "limit": 100})
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list):
            raise ValueError("Unexpected flow-list response")
        matches.extend(item for item in page if item.get("name") == payload["name"])
        if len(page) < 100:
            break
    else:
        raise ValueError(
            "Flow list exceeds safe discovery bound; refusing duplicate creation"
        )
    if len(matches) > 1:
        raise ValueError("Duplicate managed flows require explicit reconciliation")
    if matches:
        existing = matches[0]
        marker = payload["description"].splitlines()[-1]
        if marker not in (existing.get("description") or ""):
            raise ValueError("Existing flow name belongs to an unmanaged flow")
        # Repeat installation has no mutation when all managed fields match.
        if all(existing.get(key) == value for key, value in payload.items()):
            return existing
        response = client.put(f"flows/{existing['id']}", json=payload)
    else:
        response = client.post("flows", json=payload)
    response.raise_for_status()
    return response.json()


def main() -> int:
    """Render by default; install only on explicit operator request."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ai-model-id", required=True)
    parser.add_argument("--tracker-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--api-url", default=os.getenv("PRELOOP_API_URL", ""))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args()
    payload = render_flow(
        ai_model_id=args.ai_model_id,
        tracker_id=args.tracker_id,
        project_id=args.project_id,
        repository_url=args.repository_url,
        branch=args.branch,
        enable=args.enable,
    )
    if not args.apply:
        print(json.dumps(payload, indent=2))
        return 0
    parsed = urlsplit(args.api_url)
    if (
        (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1"}
            )
        )
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error(
            "--apply requires a trusted HTTPS API URL (HTTP allowed only on loopback)"
        )
    token = os.getenv("PRELOOP_API_TOKEN", "")
    if not token:
        parser.error("--apply requires PRELOOP_API_TOKEN")
    with httpx.Client(
        base_url=args.api_url.rstrip("/") + "/api/v1/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        follow_redirects=False,
    ) as client:
        result = install_flow(client, payload)
    print(
        json.dumps(
            {
                "id": result["id"],
                "name": result["name"],
                "is_enabled": result["is_enabled"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
