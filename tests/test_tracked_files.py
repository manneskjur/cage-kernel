"""A release's source archive carries only committed files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import _common
import pytest


def _git(root: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *argv],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_only_committed_files_are_listed(tmp_path: Path) -> None:
    (tmp_path / "scripts" / "__pycache__").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text("*.pyc\n.DS_Store\nnotes.md\n", encoding="utf-8")
    (tmp_path / "scripts" / "tool.py").write_text("print()\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fixture")
    # Each of these passes a clean `git status` or is untracked; none is published.
    (tmp_path / "scripts" / "__pycache__" / "tool.pyc").write_bytes(b"\0")
    (tmp_path / "scripts" / ".DS_Store").write_bytes(b"\0")
    (tmp_path / "scripts" / "notes.md").write_text("internal\n", encoding="utf-8")
    (tmp_path / "scripts" / "scratch.py").write_text("print()\n", encoding="utf-8")
    assert _common.tracked_files(tmp_path / "scripts") == {tmp_path / "scripts" / "tool.py"}


def test_a_tree_git_cannot_list_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="only from a Git checkout"):
        _common.tracked_files(tmp_path)
