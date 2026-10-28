"""Select and archive bounded, deterministic context from an isolated worktree."""

import fnmatch
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.errors import HubError
from app.git import git
from app.paths import allowed_source, is_secret, relative_path, resolve_inside
from app.projects import ProjectConfig

SECRET_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,})\b"
)


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def in_scope(path: str, patterns: list[str], project: ProjectConfig) -> bool:
    if patterns and not (
        matches(path, patterns)
        or any(
            path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
            for prefix in patterns
        )
    ):
        return False
    return (not project.include or matches(path, project.include)) and not matches(
        path, project.exclude
    )


@dataclass
class ContextBundle:
    text: str
    files: list[str]
    manifest: list[dict]
    omitted: list[dict]


def build_context(
    project: ProjectConfig,
    worktree: Path,
    scope: dict,
    task_dir: Path,
    task_id: str,
    round_no: int,
    review_base: str,
    max_bytes: int = 400_000,
    max_file_bytes: int = 200_000,
    review_type: str = "full",
) -> ContextBundle:
    all_files = [
        path.decode("utf-8") for path in git(worktree, "ls-files", "-z").split(b"\0") if path
    ]
    mode = scope["mode"]
    patterns = scope.get("paths", [])
    if mode == "module":
        module = next((m for m in project.modules if m.id == scope.get("module_id")), None)
        if module is None:
            raise HubError("module_not_found", "Requested module was not found")
        patterns = module.paths
    changed = {
        path.decode("utf-8")
        for path in git(
            worktree, "diff", "--name-only", "-z", "--no-renames", review_base, "--"
        ).split(b"\0")
        if path
    }
    selected = []
    omitted = []
    manifest = []
    sections = []
    total = 0
    for path in sorted(set(all_files) | changed):
        relative_path(path)
        if mode == "diff" and path not in changed:
            continue
        if not in_scope(path, patterns, project):
            continue
        if not allowed_source(path):
            omitted.append(
                {"path": path, "reason": "secret_name" if is_secret(path) else "excluded"}
            )
            continue
        source = resolve_inside(worktree, path)
        if not source.exists():
            omitted.append({"path": path, "reason": "deleted"})
            manifest.append({"path": path, "sha256": None, "bytes": 0, "deleted": True})
            selected.append(path)
            continue
        if not source.is_file():
            raise HubError("unsupported_file_type", f"Selected path is not a regular file: {path}")
        if source.stat().st_size > max_file_bytes:
            raise HubError("context_too_large", f"Selected file exceeds the size limit: {path}")
        content = source.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            omitted.append({"path": path, "reason": "binary"})
            continue
        if "\0" in text:
            omitted.append({"path": path, "reason": "binary"})
            continue
        if SECRET_PATTERN.search(text):
            raise HubError(
                "secret_detected", f"Possible credential found in {path}; exclude it first"
            )
        if total + len(content) > max_bytes:
            raise HubError(
                "context_too_large", "Review context exceeds the configured input budget"
            )
        digest = hashlib.sha256(content).hexdigest()
        manifest.append({"path": path, "sha256": digest, "bytes": len(content), "partial": False})
        selected.append(path)
        sections.append(json.dumps({"path": path, "sha256": digest, "content": text}))
        total += len(content)

    # Apply identical selection rules to diffs, including deleted files.
    changes = b"".join(
        git(
            worktree,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            review_base,
            "--",
            f":(literal){path}",
        )
        for path in selected
        if path in changed
    )
    if changes and SECRET_PATTERN.search(changes.decode("utf-8", errors="replace")):
        raise HubError("secret_detected", "Possible credential in review diff")
    if total + len(changes) > max_bytes:
        raise HubError("context_too_large", "Review context exceeds the input budget")
    if not selected and not changes:
        raise HubError("empty_scope", "No reviewable files or changes in the selected scope")
    text = "FILES (JSON records):\n" + "\n".join(sections)
    if changes:
        text += "\nDIFF:\n" + changes.decode("utf-8", errors="replace")
    if len(text.encode("utf-8")) > max_bytes:
        raise HubError("context_too_large", "Serialized review context exceeds the input budget")
    target = task_dir / "rounds" / str(round_no) / "context"
    target.mkdir(parents=True, exist_ok=True)
    (target / "context.txt").write_text(text, encoding="utf-8")
    (target / "manifest.json").write_text(
        json.dumps({"files": manifest, "omitted": omitted}, indent=2), encoding="utf-8"
    )
    (target / "request.json").write_text(
        json.dumps(
            {"task_id": task_id, "round": round_no, "scope": scope, "review_type": review_type}
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(target / "request.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("context.txt", "manifest.json", "request.json"):
            archive.write(target / name, name)
    return ContextBundle(text=text, files=selected, manifest=manifest, omitted=omitted)
