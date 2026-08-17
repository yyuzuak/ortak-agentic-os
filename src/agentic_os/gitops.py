from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Sequence


class GitError(RuntimeError):
    pass


def run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    input_text: str | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        text=True,
        input=input_text,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GitError(f"Command failed ({' '.join(args)}): {detail}")
    return result


class GitOperations:
    def __init__(self, root: Path, worktree_root: Path):
        self.root = root.resolve()
        self.worktree_root = worktree_root.resolve()

    def ensure_repository(self) -> None:
        result = run_command(
            ["git", "rev-parse", "--show-toplevel"], cwd=self.root, check=False
        )
        if result.returncode != 0:
            raise GitError(f"Not a Git repository: {self.root}")
        if Path(result.stdout.strip()).resolve() != self.root:
            raise GitError("agentic.yaml must be located at the Git repository root")

    def head(self, cwd: Path | None = None) -> str:
        return run_command(["git", "rev-parse", "HEAD"], cwd=cwd or self.root).stdout.strip()

    def branch(self, cwd: Path) -> str:
        return run_command(
            ["git", "branch", "--show-current"], cwd=cwd
        ).stdout.strip()

    def is_clean(self, cwd: Path) -> bool:
        return not bool(self.status_paths(cwd))

    def status_paths(self, cwd: Path) -> list[str]:
        output = run_command(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=cwd
        ).stdout
        paths: list[str] = []
        for line in output.splitlines():
            if not line:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            paths.append(path)
        return sorted(paths)

    def create_worktree(
        self, name: str, branch: str, *, base_ref: str = "HEAD"
    ) -> tuple[Path, str]:
        self.ensure_repository()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
            raise GitError("Worktree name must be a lowercase slug")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch):
            raise GitError("Invalid branch name")
        path = self.worktree_root / name
        if path.exists():
            raise GitError(f"Worktree path already exists: {path}")
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        base_sha = run_command(
            ["git", "rev-parse", "--verify", base_ref], cwd=self.root
        ).stdout.strip()
        run_command(
            ["git", "worktree", "add", "-b", branch, str(path), base_sha], cwd=self.root
        )
        return path, base_sha

    def remove_worktree(self, path: Path) -> None:
        if not path.exists():
            raise GitError(f"Worktree path does not exist: {path}")
        if not self.is_clean(path):
            changed = ", ".join(self.status_paths(path))
            raise GitError(f"Worktree contains uncommitted changes: {changed}")
        run_command(["git", "worktree", "remove", str(path)], cwd=self.root)

    def commit_checkpoint(self, cwd: Path, message: str) -> tuple[str, list[str]]:
        paths = self.status_paths(cwd)
        if not paths:
            return self.head(cwd), []
        run_command(["git", "add", "-A"], cwd=cwd)
        run_command(["git", "commit", "-m", message], cwd=cwd)
        return self.head(cwd), paths

    def reset_hard(self, cwd: Path, sha: str) -> None:
        run_command(["git", "reset", "--hard", sha], cwd=cwd)

    def discard_changes(self, cwd: Path) -> list[str]:
        """Throw away everything not committed. Returns what was discarded."""
        discarded = self.status_paths(cwd)
        if not discarded:
            return []
        run_command(["git", "reset", "--hard", "HEAD"], cwd=cwd)
        run_command(["git", "clean", "-fd"], cwd=cwd)
        return discarded

    def changed_since(self, cwd: Path, base_sha: str) -> list[str]:
        output = run_command(
            ["git", "diff", "--name-only", f"{base_sha}..HEAD"], cwd=cwd
        ).stdout
        return sorted(path for path in output.splitlines() if path)

    def create_integration(
        self,
        *,
        integration_root: Path,
        integration_branch: str,
        base_sha: str,
        source_branch: str,
    ) -> tuple[Path, str, str]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", integration_branch):
            raise GitError("Invalid integration branch name")
        path = integration_root / integration_branch.replace("/", "-")
        integration_root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self.branch(path) != integration_branch:
                raise GitError(
                    f"Integration path is on a different branch: {path}"
                )
            if not self.is_clean(path):
                raise GitError(f"Integration worktree contains unresolved changes: {path}")
        else:
            existing = run_command(
                ["git", "show-ref", "--verify", f"refs/heads/{integration_branch}"],
                cwd=self.root,
                check=False,
            )
            if existing.returncode == 0:
                run_command(
                    ["git", "worktree", "add", str(path), integration_branch],
                    cwd=self.root,
                )
            else:
                run_command(
                    [
                        "git",
                        "worktree",
                        "add",
                        "-b",
                        integration_branch,
                        str(path),
                        base_sha,
                    ],
                    cwd=self.root,
                )
        before_merge = self.head(path)
        merge = run_command(
            ["git", "merge", "--no-ff", "--no-edit", source_branch],
            cwd=path,
            check=False,
        )
        if merge.returncode != 0:
            # Leave the integration worktree usable: an abandoned conflict would
            # block every later goal that targets this sprint branch.
            run_command(["git", "merge", "--abort"], cwd=path, check=False)
            self.reset_hard(path, before_merge)
            detail = merge.stdout.strip() or merge.stderr.strip()
            raise GitError(f"Merge failed ({source_branch} into {integration_branch}): {detail}")
        return path, self.head(path), before_merge
