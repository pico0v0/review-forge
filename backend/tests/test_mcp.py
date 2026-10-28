import importlib.util
from pathlib import Path


def load_server():
    path = Path(__file__).parents[1] / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("reviewforge_mcp_server", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_mcp_exposes_review_tools() -> None:
    module = load_server()
    assert set(module.mcp._tool_manager._tools) == {
        "review_submit",
        "review_status",
        "review_wait",
        "review_result",
        "review_history",
        "review_cancel",
        "review_models",
        "review_fix_instructions",
        "review_retry",
    }


def test_retry_explains_current_hub_boundary() -> None:
    module = load_server()
    result = module.review_retry("REV-20261028-001")
    assert result["ok"] is False
    assert result["error"]["code"] == "feature_not_available"
