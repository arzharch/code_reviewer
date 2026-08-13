"""
Plan-and-execute wiring: fan-out, fan-in, and the bounded replan loop.
"""
from langgraph.types import Send

from src.agent import agent as agent_module
from src.agent.state import Finding, ProjectProfile, Proposal, ReviewJob, TestResult

PROFILE = ProjectProfile(
    primary_language="python",
    package_manager="pip",
    test_command="pytest",
    detection_confidence=1.0,
    detection_sources=["pyproject.toml"],
)


def _finding(fid="f1"):
    return Finding(
        id=fid, tool="ruff", file="x.py", line_range=(1, 1),
        severity="warning", category="lint", description="unused import",
    )


def _proposal(fid="f1"):
    return Proposal(finding_id=fid, diff="d", description="fix", rationale="r", confidence=0.9)


def _result(fid, passed, attempt=0):
    return TestResult(proposal_id=fid, passed=passed, output="out", attempt=attempt)


class TestGraphShape:
    def test_topology_is_not_linear(self):
        graph = agent_module.get_compiled_graph().get_graph()
        edges = {(e.source, e.target) for e in graph.edges}
        # The replan loop and the fan-out are what make this plan-and-execute.
        assert ("replan", "draft_fixes") in edges
        assert ("draft_fixes", "verify_proposal") in edges
        assert ("verify_proposal", "collect") in edges
        assert any(e.conditional for e in graph.edges)


class TestFanOut:
    def test_one_branch_per_pending_proposal(self):
        state = {
            "job": ReviewJob(repo="/tmp/r"),
            "profile": PROFILE,
            "findings": [_finding("f1"), _finding("f2")],
            "proposals": [_proposal("f1"), _proposal("f2")],
            "pending_finding_ids": ["f1", "f2"],
        }
        sends = agent_module.fan_out_proposals(state)
        assert len(sends) == 2
        assert all(isinstance(s, Send) and s.node == "verify_proposal" for s in sends)
        assert {s.arg["proposal"].finding_id for s in sends} == {"f1", "f2"}

    def test_only_pending_proposals_are_reverified(self):
        # After a replan round, proposals that already passed must not re-run.
        state = {
            "job": ReviewJob(repo="/tmp/r"),
            "profile": PROFILE,
            "findings": [_finding("f1"), _finding("f2")],
            "proposals": [_proposal("f1"), _proposal("f2")],
            "pending_finding_ids": ["f2"],
        }
        sends = agent_module.fan_out_proposals(state)
        assert [s.arg["proposal"].finding_id for s in sends] == ["f2"]

    def test_no_proposals_skips_straight_to_collect(self):
        state = {"job": ReviewJob(repo="/tmp/r"), "profile": PROFILE,
                 "findings": [], "proposals": [], "pending_finding_ids": []}
        assert agent_module.fan_out_proposals(state) == "collect"


class TestFanIn:
    def test_latest_attempt_wins(self):
        state = {"test_results": [_result("f1", False, 0), _result("f1", True, 1)]}
        latest = agent_module.latest_result_per_proposal(state)
        assert latest["f1"].passed

    def test_collect_lists_failures_including_unappliable_patches(self):
        state = {
            "test_results": [_result("f1", True), _result("f2", False)],
            "unapplied": {"f3": "bad patch"},
        }
        pending = agent_module.collect(state)["pending_finding_ids"]
        assert set(pending) == {"f2", "f3"}


class TestReplanRouting:
    def test_failures_trigger_a_replan(self, monkeypatch):
        monkeypatch.setattr(agent_module.settings, "max_replan_rounds", 1)
        state = {"pending_finding_ids": ["f2"], "replan_rounds": 0}
        assert agent_module.route_after_collect(state) == "replan"

    def test_replan_budget_is_bounded(self, monkeypatch):
        monkeypatch.setattr(agent_module.settings, "max_replan_rounds", 1)
        state = {"pending_finding_ids": ["f2"], "replan_rounds": 1}
        assert agent_module.route_after_collect(state) == "risk_score"

    def test_clean_run_goes_straight_to_scoring(self):
        assert agent_module.route_after_collect({"pending_finding_ids": [], "replan_rounds": 0}) == "risk_score"


class TestRiskScoring:
    def test_unverified_proposal_is_escalated(self):
        state = {
            "job": ReviewJob(repo="/tmp/r"),
            "proposals": [_proposal("f1")],
            "findings": [_finding("f1")],
            "test_results": [],
            "diff_files": ["x.py"],
        }
        assessment = agent_module.risk_score(state)["risk_assessments"][0]
        assert assessment.decision == "escalate"
        assert "never verified" in assessment.reasons[0]

    def test_each_proposal_is_scored_against_its_own_test_run(self, monkeypatch):
        monkeypatch.setattr(agent_module.settings, "auto_commit_threshold", 0.5)
        state = {
            "job": ReviewJob(repo="/tmp/r"),
            "proposals": [_proposal("f1"), _proposal("f2")],
            "findings": [_finding("f1"), _finding("f2")],
            "test_results": [_result("f1", True), _result("f2", False)],
            "diff_files": ["x.py"],
        }
        by_id = {a.proposal_id: a for a in agent_module.risk_score(state)["risk_assessments"]}
        assert by_id["f1"].decision == "auto_commit"
        assert by_id["f2"].decision == "escalate"

    def test_repair_attempts_are_recorded_on_the_assessment(self, monkeypatch):
        monkeypatch.setattr(agent_module.settings, "auto_commit_threshold", 0.5)
        state = {
            "job": ReviewJob(repo="/tmp/r"),
            "proposals": [_proposal("f1")],
            "findings": [_finding("f1")],
            "test_results": [_result("f1", True, attempt=2)],
            "retries_used": {"f1": 2},
            "diff_files": ["x.py"],
        }
        assessment = agent_module.risk_score(state)["risk_assessments"][0]
        assert any("2 repair attempt" in r for r in assessment.reasons)


class TestPlanning:
    def test_planner_falls_back_to_a_deterministic_plan(self, monkeypatch):
        # An LLM outage must not silently drop every finding.
        monkeypatch.setattr(agent_module.settings, "openai_api_key",
                            type("S", (), {"get_secret_value": lambda self: "sk-test"})())

        class Boom:
            def with_structured_output(self, _schema):
                return self

            def __or__(self, other):
                return self

            def __ror__(self, other):
                return self

            def invoke(self, *_args, **_kwargs):
                raise RuntimeError("openai down")

        monkeypatch.setattr(agent_module, "ChatOpenAI", lambda **kwargs: Boom())
        state = {"findings": [_finding("f1"), _finding("f2")], "profile": PROFILE}
        out = agent_module.plan(state)
        assert set(out["pending_finding_ids"]) == {"f1", "f2"}
