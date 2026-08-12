import os
import subprocess

import pytest

from src.agent.state import Proposal
from src.execution_plane.sandbox import apply_patches, build_sandbox_env, normalize_diff


def _proposal(finding_id: str, diff: str) -> Proposal:
    return Proposal(
        finding_id=finding_id,
        diff=diff,
        description="desc",
        rationale="rationale",
        confidence=0.9,
    )


@pytest.fixture
def repo(tmp_path):
    """A one-file git repository."""
    (tmp_path / "x.py").write_text("a = 1\n")
    env = build_sandbox_env(str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=env, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=env, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path, env=env, check=True,
    )
    return tmp_path


VALID_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"


class TestNormalizeDiff:
    def test_rejects_empty(self):
        assert normalize_diff("") is None

    def test_rejects_prose_snippets(self):
        # The failure mode seen on live PRs: the model returns before/after
        # prose instead of a patch.
        assert normalize_diff("# Original code\nfoo()\n# Proposed fix\nbar()") is None

    def test_strips_markdown_fence(self):
        out = normalize_diff(f"```diff\n{VALID_DIFF}```")
        assert out is not None
        assert out.startswith("--- a/x.py")
        assert "```" not in out

    def test_normalizes_line_endings(self):
        out = normalize_diff(VALID_DIFF.replace("\n", "\r\n"))
        assert out is not None
        assert "\r" not in out
        assert out.endswith("\n")


class TestApplyPatches:
    def test_applies_valid_patch(self, repo):
        result = apply_patches(str(repo), [_proposal("ok", VALID_DIFF)])
        assert result.applied == ["ok"]
        assert result.all_applied
        assert (repo / "x.py").read_text() == "a = 2\n"

    def test_one_bad_patch_does_not_discard_the_others(self, repo):
        result = apply_patches(str(repo), [
            _proposal("prose", "just write it better"),
            _proposal("ok", VALID_DIFF),
            _proposal("stale", "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-zzz\n+yyy\n"),
        ])
        assert result.applied == ["ok"]
        assert set(result.failed) == {"prose", "stale"}
        assert (repo / "x.py").read_text() == "a = 2\n"

    def test_does_not_leave_patch_files_in_the_tree(self, repo):
        apply_patches(str(repo), [_proposal("ok", VALID_DIFF)])
        assert not [f for f in os.listdir(repo) if f.endswith(".diff")]

    def test_works_on_a_plain_directory(self, tmp_path):
        # A sandbox copy has no .git; apply_patches must bootstrap one.
        (tmp_path / "x.py").write_text("a = 1\n")
        result = apply_patches(str(tmp_path), [_proposal("ok", VALID_DIFF)])
        assert result.applied == ["ok"]


class TestSandboxEnv:
    def test_secrets_are_not_inherited(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
        monkeypatch.setenv("GITHUB_PRIVATE_KEY", "-----BEGIN RSA-----")
        env = build_sandbox_env(str(tmp_path))
        assert "OPENAI_API_KEY" not in env
        assert "GITHUB_PRIVATE_KEY" not in env

    def test_keeps_what_subprocesses_need(self, tmp_path):
        env = build_sandbox_env(str(tmp_path))
        assert "PATH" in env
        assert env["HOME"] == str(tmp_path)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
