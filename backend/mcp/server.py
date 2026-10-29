"""Small MCP stdio client for the local ReviewForge HTTP API."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ai-review-hub")


def _settings() -> tuple[str, str]:
    default_config = Path(__file__).resolve().parents[2] / "config"
    config_dir = Path(os.environ.get("AIH_CONFIG_DIR", default_config)).expanduser().resolve()
    config_path = config_dir / "config.yaml"
    data_dir = Path.home() / ".reviewforge" / "data"
    if config_path.is_file():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        configured = (raw.get("paths") or {}).get("data_dir")
        if configured:
            data_dir = Path(configured).expanduser()
    token_path = data_dir / "runtime" / "http_token"
    if not token_path.is_file():
        raise RuntimeError("ReviewForge Hub token is unavailable; start the Hub first")
    base_url = os.environ.get("REVIEWFORGE_HUB_URL", "http://127.0.0.1:8787").rstrip("/")
    return base_url, token_path.read_text(encoding="utf-8").strip()


def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    try:
        base_url, token = _settings()
        response = httpx.request(
            method,
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            **kwargs,
        )
    except (OSError, httpx.HTTPError) as exc:
        return {"ok": False, "error": {"code": "hub_unavailable", "message": str(exc)}}
    if response.headers.get("content-type", "").startswith("text/"):
        payload = response.text
    else:
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:1000]}
    if response.is_error:
        return {
            "ok": False,
            "error": payload.get("error", {"code": "hub_error", "message": str(payload)})
            if isinstance(payload, dict)
            else {"code": "hub_error", "message": str(payload)},
        }
    return {"ok": True, "data": payload}


@mcp.tool()
def review_submit(
    project_id: str,
    scope: dict[str, Any],
    reviewer: dict[str, Any] | None = None,
    fixer: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Submit a review task and return its task ID immediately."""
    body: dict[str, Any] = {"project_id": project_id, "scope": scope, "dry_run": dry_run}
    if reviewer is not None:
        body["reviewer"] = reviewer
    if fixer is not None:
        body["fixer"] = fixer
    if limits is not None:
        body["limits"] = limits
    result = _request("POST", "/api/reviews", json=body)
    if result.get("ok"):
        task = result["data"]
        result["data"] = {
            "task_id": task["id"],
            "status": task["status"],
            "max_rounds": task["max_rounds"],
            "reviewer": task["reviewer"],
            "fixer": task["fixer"],
        }
    return result


@mcp.tool()
def review_status(task_id: str) -> dict[str, Any]:
    """Return task status, round, stop reason, and model usage."""
    result = _request("GET", f"/api/reviews/{task_id}")
    if result.get("ok"):
        task = result["data"]
        result["data"] = {
            "task_id": task["id"],
            "status": task["status"],
            "round": task["current_round"],
            "max_rounds": task["max_rounds"],
            "stop_reason": task["stop_reason"],
            "usage": {"tokens": task["tokens_used"], "cost_usd": task["cost_usd"]},
            "last_error": task["error_message"],
        }
    return result


@mcp.tool()
def review_wait(task_id: str, timeout_sec: int = 120) -> dict[str, Any]:
    """Poll a task until it finishes or timeout_sec (maximum 120) elapses."""
    deadline = time.monotonic() + min(max(timeout_sec, 0), 120)
    result = review_status(task_id)
    while (
        result.get("ok")
        and result["data"]["status"]
        in {
            "CREATED",
            "BUILDING_CONTEXT",
            "REVIEWING",
            "FIXING",
            "VALIDATING",
            "FINALIZING",
        }
        and time.monotonic() < deadline
    ):
        time.sleep(0.5)
        result = review_status(task_id)
    return result


@mcp.tool()
def review_result(task_id: str, include_report: bool = False) -> dict[str, Any]:
    """Read the final task result, findings, validation, and optional report."""
    status = _request("GET", f"/api/reviews/{task_id}")
    if not status.get("ok"):
        return status
    result = status["data"]
    findings = _request("GET", f"/api/reviews/{task_id}/findings")
    rounds = _request("GET", f"/api/reviews/{task_id}/rounds")
    result["findings"] = findings.get("data", []) if findings.get("ok") else []
    result["rounds"] = rounds.get("data", []) if rounds.get("ok") else []
    if include_report:
        report = _request("GET", f"/api/reviews/{task_id}/report")
        result["report"] = report.get("data", "") if report.get("ok") else None
    return {"ok": True, "data": result}


@mcp.tool()
def review_history(project_id: str, limit: int = 20) -> dict[str, Any]:
    """List recent review tasks for a project."""
    return _request("GET", "/api/reviews", params={"project_id": project_id, "limit": limit})


@mcp.tool()
def review_cancel(task_id: str) -> dict[str, Any]:
    """Cancel an active review task."""
    return _request("POST", f"/api/reviews/{task_id}/cancel")


@mcp.tool()
def review_models(project_id: str | None = None) -> dict[str, Any]:
    """List configured model providers; project_id is accepted for tool compatibility."""
    del project_id
    return _request("GET", "/api/models")


@mcp.tool()
def review_fix_instructions(task_id: str, severities: list[str] | None = None) -> dict[str, Any]:
    """Return concise follow-up guidance from the persisted findings."""
    result = _request("GET", f"/api/reviews/{task_id}/findings")
    if not result.get("ok"):
        return result
    selected = result["data"]
    if severities:
        selected = [item for item in selected if item.get("severity") in severities]
    return {
        "ok": True,
        "data": {
            "task_id": task_id,
            "findings": selected,
            "instructions": [
                f"Address {item.get('severity')} finding {item.get('id')} in {item.get('file')}"
                for item in selected
            ],
        },
    }


@mcp.tool()
def review_retry(task_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retry a stopped review, optionally overriding models, limits, or dry_run."""
    return _request("POST", f"/api/reviews/{task_id}/retry", json=overrides or {})


if __name__ == "__main__":
    mcp.run(transport="stdio")
