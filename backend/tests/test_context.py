import subprocess
from pathlib import Path

import pytest

from app.context import build_context
from app.errors import HubError
from app.projects import ProjectConfig


def init_repo(root: Path) -> str:
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True
    )
    (root / "main.py").write_text("print('safe')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def test_build_context_archives_manifest_and_source(tmp_path: Path) -> None:
    root = tmp_path / "project"
    base = init_repo(root)
    (root / "main.py").write_text("print('review me')\n", encoding="utf-8")
    task_dir = tmp_path / "task"
    project = ProjectConfig(id="demo", name="Demo", root=root)

    bundle = build_context(project, root, {"mode": "all"}, task_dir, "REV-1", 1, base)

    assert bundle.files == ["main.py"]
    assert "review me" in bundle.text
    assert (task_dir / "rounds/1/context/request.zip").is_file()


def test_build_context_omits_secret_names_and_blocks_secret_content(tmp_path: Path) -> None:
    root = tmp_path / "project"
    base = init_repo(root)
    (root / ".env.local").write_text("API_KEY=example", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", ".env.local"], check=True)
    project = ProjectConfig(id="demo", name="Demo", root=root)

    bundle = build_context(project, root, {"mode": "all"}, tmp_path / "task", "REV-1", 1, base)
    assert any(item["reason"] == "secret_name" for item in bundle.omitted)

    (root / "main.py").write_text("TOKEN = 'sk-" + "A" * 32 + "'\n", encoding="utf-8")
    with pytest.raises(HubError, match="Possible credential"):
        build_context(project, root, {"mode": "all"}, tmp_path / "task2", "REV-2", 1, base)


def test_paths_scope_filters_files_and_diffs(tmp_path: Path) -> None:
    root = tmp_path / "project"
    base = init_repo(root)
    (root / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "other.py"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "add other"], check=True)
    review_base = base
    project = ProjectConfig(id="demo", name="Demo", root=root)
    (root / "main.py").write_text("MAIN = 2\n", encoding="utf-8")
    (root / "other.py").write_text("OTHER = 2\n", encoding="utf-8")

    bundle = build_context(
        project,
        root,
        {"mode": "paths", "paths": ["main.py"]},
        tmp_path / "task",
        "REV-1",
        1,
        review_base,
    )

    assert bundle.files == ["main.py"]
    assert "MAIN = 2" in bundle.text
    assert "OTHER = 2" not in bundle.text
    assert "other.py" not in bundle.text
