from pathlib import Path

import pytest

from app.projects import ProjectConfig, ProjectConfigError, ProjectRegistry


def initialize_git_repo(path: Path) -> None:
    path.mkdir()
    import subprocess

    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


def test_project_registry_round_trips_and_writes_atomically(tmp_path: Path) -> None:
    root = tmp_path / "project"
    initialize_git_repo(root)
    prompt = tmp_path / "prompts" / "reviewer.md"
    prompt.parent.mkdir()
    prompt.write_text("Review {{ context }}", encoding="utf-8")
    registry = ProjectRegistry(tmp_path / "config")

    project = ProjectConfig(
        id="demo-project",
        name="Demo",
        root=root,
        review_prompt=prompt.relative_to(tmp_path).as_posix(),
    )
    registry.register(project)

    loaded = registry.get("demo-project")
    assert loaded is not None
    assert loaded.root == root.resolve()
    assert registry.path.exists()


def test_project_registry_rejects_non_git_root(tmp_path: Path) -> None:
    prompt = tmp_path / "reviewer.md"
    prompt.write_text("Review", encoding="utf-8")
    registry = ProjectRegistry(tmp_path / "config")
    project = ProjectConfig(
        id="demo", name="Demo", root=tmp_path, review_prompt=prompt.relative_to(tmp_path).as_posix()
    )
    with pytest.raises(ProjectConfigError, match="Git repository"):
        registry.register(project)


def test_project_registry_updates_without_changing_id(tmp_path: Path) -> None:
    root = tmp_path / "project"
    initialize_git_repo(root)
    prompt = tmp_path / "reviewer.md"
    prompt.write_text("Review", encoding="utf-8")
    registry = ProjectRegistry(tmp_path / "config")
    project = ProjectConfig(
        id="demo", name="Demo", root=root, review_prompt=prompt.relative_to(tmp_path).as_posix()
    )
    registry.register(project)
    changed = project.model_copy(update={"name": "Updated Demo"})

    registry.update("demo", changed)

    assert registry.get("demo").name == "Updated Demo"
    with pytest.raises(ProjectConfigError, match="cannot be changed"):
        registry.update("demo", changed.model_copy(update={"id": "renamed"}))


def test_project_registry_rejects_prompt_outside_hub_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    initialize_git_repo(root)
    prompt = tmp_path / "reviewer.md"
    prompt.write_text("Review", encoding="utf-8")
    registry = ProjectRegistry(tmp_path / "config")
    project = ProjectConfig(id="demo", name="Demo", root=root, review_prompt="../reviewer.md")

    with pytest.raises(ProjectConfigError, match="inside the Hub root"):
        registry.register(project)


def test_project_model_rejects_relative_root() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ProjectConfig(id="demo", name="Demo", root="relative", review_prompt="reviewer.md")


@pytest.mark.parametrize("path", [r"..\outside\**", "../outside", "C:relative", "/absolute"])
def test_module_paths_reject_traversal(tmp_path, path) -> None:
    with pytest.raises(ValueError, match="relative patterns"):
        ProjectConfig(
            id="demo",
            name="Demo",
            root=tmp_path,
            modules=[{"id": "module", "name": "Module", "paths": [path]}],
        )
