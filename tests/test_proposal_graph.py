"""
The per-proposal sandbox subgraph: one tree per proposal, bounded repairs.
"""
import pytest

from src.agent import proposal_graph
from src.agent.state import ProjectProfile, Proposal, ReviewJob, TestResult
from src.execution_plane.sandbox import ApplyResult

PROFILE = ProjectProfile(
    primary_language="python",
    package_manager="pip",
    test_command="pytest",
    detection_confidence=1.0,
    detection_sources=["pyproject.toml"],
)


def _proposal(finding_id="f1"):
    return Proposal(
        finding_id=finding_id,
        diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
        description="fix it",
        rationale="because",
        confidence=0.9,
    )


class FakeSandbox:
    """Sandbox stub whose test outcome is scripted per attempt."""
    instances = 0

    def __init__(self, repo):
        FakeSandbox.instances += 1
        self.repo = repo

    def setup(self):
        pass

    def teardown(self):
        pass

    def apply_proposals(self, proposals):
        if FakeSandbox.apply_fails:
            return ApplyResult(failed={p.finding_id: "does not apply" for p in proposals})
        return ApplyResult(applied=[p.finding_id for p in proposals])

    def run_tests(self, profile):
        passed = FakeSandbox.results.pop(0) if FakeSandbox.results else True
        return TestResult(passed=passed, output="pass" if passed else "assert failed")


@pytest.fixture
def sandbox(monkeypatch):
    FakeSandbox.instances = 0
    FakeSandbox.results = []
    FakeSandbox.apply_fails = False
    monkeypatch.setattr(proposal_graph, "SandboxRuntime", FakeSandbox)
    # No API key: `repair` still consumes budget but skips the LLM round trip.
    monkeypatch.setattr(proposal_graph.settings, "openai_api_key", None)
    monkeypatch.setattr(proposal_graph.settings, "max_repair_attempts", 2)
    return FakeSandbox


def _run(proposal=None):
    graph = proposal_graph.build_proposal_graph()
    return graph.invoke({
        "job": ReviewJob(repo="/tmp/repo"),
        "profile": PROFILE,
        "proposal": proposal or _proposal(),
        "attempt": 0,
    })


def test_passing_proposal_needs_no_repair(sandbox):
    sandbox.results = [True]
    out = _run()
    assert len(out["test_results"]) == 1
    assert out["test_results"][0].passed
    assert out.get("retries_used", {}) == {}
    assert sandbox.instances == 1


def test_repairs_are_bounded_by_the_budget(sandbox):
    # Always fails: the loop must stop after max_repair_attempts repairs.
    sandbox.results = [False] * 10
    out = _run()
    assert len(out["test_results"]) == 3  # initial attempt + 2 repairs
    assert [r.attempt for r in out["test_results"]] == [0, 1, 2]
    assert not any(r.passed for r in out["test_results"])


def test_stops_as_soon_as_a_repair_succeeds(sandbox):
    sandbox.results = [False, True]
    out = _run()
    assert len(out["test_results"]) == 2
    assert out["test_results"][-1].passed


def test_only_shared_channels_reach_the_parent(sandbox):
    sandbox.results = [True]
    out = _run()
    # `proposal`, `attempt` and `last_error` are subgraph-internal.
    assert set(out).issubset({"proposals", "test_results", "unapplied", "retries_used"})


def test_unappliable_patch_is_reported_and_not_tested(sandbox):
    sandbox.apply_fails = True
    out = _run()
    assert out["unapplied"] == {"f1": "does not apply"}
    assert out["test_results"][0].passed is False
    assert "did not apply" in out["test_results"][0].output


def test_successful_apply_retracts_an_earlier_failure(sandbox):
    from src.agent.state import merge_failures
    # First attempt failed to apply, second applied cleanly.
    assert merge_failures({"f1": "does not apply"}, {"f1": ""}) == {}
