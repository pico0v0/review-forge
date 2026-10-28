import json

import pytest

from app.errors import HubError
from app.protocol import parse_response


def base_response():
    return {
        "schema_version": "2.0",
        "task_id": "REV-20261028-001",
        "round": 1,
        "verdict": "approved",
        "summary": "OK",
        "findings": [],
    }


def test_response_rejects_wrong_task_and_mutually_exclusive_changes():
    with pytest.raises(ValueError, match="requested task"):
        parse_response(json.dumps(base_response()), "other", 1)
    body = {**base_response(), "patch": "example", "edits": [{"path": "a.py", "content": ""}]}
    with pytest.raises(ValueError, match="mutually exclusive"):
        parse_response(json.dumps(body), "REV-20261028-001", 1)


@pytest.mark.parametrize("path", ["../outside", r"..\outside", "C:stream", ".git/config"])
def test_response_rejects_unsafe_paths(path):
    body = {**base_response(), "edits": [{"path": path, "content": ""}]}
    if path == ".git/config":
        # Protocol validates syntax; policy additionally excludes repository metadata.
        from app.paths import allowed_source

        assert not allowed_source(path)
        return
    with pytest.raises((ValueError, HubError)):
        parse_response(json.dumps(body), "REV-20261028-001", 1)
