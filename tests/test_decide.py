"""
Covers the auto-commit / escalate split in aggregate_and_decide.

This is the path the project claims as its headline feature, so every branch
of it is asserted: clean auto-commit, mixed batch, re-verification failure,
push failure, and local runs which must never write to a developer's checkout.
"""
import pytest

from src.agent import agent as agent_module
from src.agent.state import (
    ProjectProfile,
    Proposal,
    RiskAssessment,
    ReviewJob,
    TestResult,
)
from src.execution_plane.sandbox import ApplyResult


def _proposal(finding_id: str) -> Proposal:
    return Proposal(
        finding_id=finding_id,
        diff=f"--- a/{finding_id}.py\n+++ b/{finding_id}.py\n@@ -1 +1 @@\n-a\n+b\n",
        description=f"fix {finding_id}",
        rationale="because",
        confidence=0.95,
    )


def _assessment(finding_id: str, decision: str) -> RiskAssessment:
    return RiskAssessment(
        proposal_id=finding_id,
        score=0.0 if decision == "auto_commit" else 0.9,
        signals={},
        decision=decision,
        reasons=[] if decision == "auto_commit" else ["too risky"],
    )


PROFILE = ProjectProfile(
    primary_language="python",
    package_manager="pip",
    test_command="pytest",
    detection_confidence=1.0,
    detection_sources=["pyproject.toml"],
)


@pytest.fixture
def pr_job():
    return ReviewJob(
        repo="/tmp/agent_workspace_x",
        repo_full_name="owner/repo",
        pr_number=7,
        branch="feature",
        installation_id=1,
        workspace_is_clone=True,
    )


@pytest.fixture
def spy(monkeypatch):
    """Records pushes and comments instead of talking to GitHub."""
    calls = {"pushed": [], "comments": []}

    def fake_push(repo_path, proposals, branch):
        calls["pushed"].append((repo_path, [p.finding_id for p in proposals], branch))
        return calls.get("push_ok", True)

    def fake_comment(repo_full_name, pr_number, comment, installation_id=None):
        calls["comments"].append(comment)
        return True

    monkeypatch.setattr(agent_module.GitActionsService, "commit_and_push", staticmethod(fake_push))
    monkeypatch.setattr(agent_module.GitActionsService, "post_pr_comment", staticmethod(fake_comment))
    monkeypatch.setattr(
        agent_module, "verify_proposals",
        lambda repo, profile, proposals: (TestResult(passed=True, output="ok"), {}),
    )
    monkeypatch.setattr(
        agent_module, "apply_patches",
        lambda repo, proposals, env=None: ApplyResult(applied=[p.finding_id for p in proposals]),
    )
    return calls


def _state(job, proposals, assessments):
    return {
        "job": job,
        "profile": PROFILE,
        "proposals": proposals,
        "risk_assessments": assessments,
        "findings": [],
    }


def test_low_risk_batch_is_pushed(pr_job, spy):
    props = [_proposal("a"), _proposal("b")]
    result = agent_module.aggregate_and_decide(
        _state(pr_job, props, [_assessment("a", "auto_commit"), _assessment("b", "auto_commit")])
    )
    assert result["status"] == "auto_committed"
    assert spy["pushed"] == [(pr_job.repo, ["a", "b"], "feature")]
    assert "fixes pushed" in spy["comments"][0]


def test_mixed_batch_pushes_safe_and_escalates_risky(pr_job, spy):
    props = [_proposal("safe"), _proposal("risky")]
    result = agent_module.aggregate_and_decide(
        _state(pr_job, props, [_assessment("safe", "auto_commit"), _assessment("risky", "escalate")])
    )
    assert result["status"] == "partially_committed"
    assert spy["pushed"] == [(pr_job.repo, ["safe"], "feature")]
    comment = spy["comments"][0]
    assert "fixes pushed" in comment and "needs a human" in comment


def test_one_escalation_no_longer_blocks_every_other_fix(pr_job, spy):
    # Previously `any(escalate)` escalated the entire batch.
    props = [_proposal("safe1"), _proposal("safe2"), _proposal("risky")]
    agent_module.aggregate_and_decide(_state(pr_job, props, [
        _assessment("safe1", "auto_commit"),
        _assessment("safe2", "auto_commit"),
        _assessment("risky", "escalate"),
    ]))
    assert spy["pushed"][0][1] == ["safe1", "safe2"]


def test_subset_failing_reverification_is_escalated(pr_job, spy, monkeypatch):
    monkeypatch.setattr(
        agent_module, "verify_proposals",
        lambda repo, profile, proposals: (TestResult(passed=False, output="boom"), {}),
    )
    result = agent_module.aggregate_and_decide(
        _state(pr_job, [_proposal("a")], [_assessment("a", "auto_commit")])
    )
    assert result["status"] == "escalated"
    assert spy["pushed"] == []
    assert "Test suite failed" in spy["comments"][0]


def test_failed_push_does_not_report_success(pr_job, spy):
    spy["push_ok"] = False
    result = agent_module.aggregate_and_decide(
        _state(pr_job, [_proposal("a")], [_assessment("a", "auto_commit")])
    )
    assert result["status"] == "escalated"
    comment = spy["comments"][0]
    assert "fixes pushed" not in comment
    assert "Commit or push to the PR branch failed." in comment


def test_local_run_never_writes_to_the_developer_checkout(spy):
    local_job = ReviewJob(repo="/home/dev/project", workspace_is_clone=False)
    result = agent_module.aggregate_and_decide(
        _state(local_job, [_proposal("a")], [_assessment("a", "auto_commit")])
    )
    assert result["status"] == "escalated"
    assert spy["pushed"] == []


def test_unapplied_patch_is_escalated_by_risk_score():
    state = {
        "job": ReviewJob(repo="/tmp/x"),
        "proposals": [_proposal("broken")],
        "test_results": [TestResult(passed=True, output="ok")],
        "findings": [],
        "diff_files": ["x.py"],
        "unapplied": {"broken": "corrupt patch"},
    }
    assessments = agent_module.risk_score(state)["risk_assessments"]
    assert assessments[0].decision == "escalate"
    assert "corrupt patch" in assessments[0].reasons[0]
