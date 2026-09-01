"""
Analysis must degrade, not explode: missing tools and missing credentials are
normal operating conditions in a container, and the token budget is a hard
ceiling because reviews run unattended.
"""
from langchain_core.runnables import RunnableLambda

from src.agent.analysis import LLMAnalysisService, StaticAnalysisService


class TestStaticAnalysis:
    def test_scopes_tools_to_changed_python_files(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "notes.md").write_text("# hi\n")
        service = StaticAnalysisService(str(tmp_path), ["a.py", "notes.md", "gone.py"])
        assert service._python_targets(service.diff_files) == ["a.py"]

    def test_missing_tool_yields_no_findings_instead_of_raising(self, tmp_path, monkeypatch):
        def boom(*_args, **_kwargs):
            raise FileNotFoundError("ruff")

        monkeypatch.setattr("src.agent.analysis.subprocess.run", boom)
        assert StaticAnalysisService(str(tmp_path), []).run_ruff() == []


class TestLLMAnalysis:
    def test_no_api_key_skips_the_semantic_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.analysis.settings.openai_api_key", None)
        service = LLMAnalysisService(str(tmp_path), ["a.py"])
        # Must not construct a client (which would raise) or make a call.
        assert service.run_semantic_analysis() == []
        assert service.tokens_used == 0

    def test_token_budget_stops_the_scan_without_prompting(self, tmp_path, monkeypatch):
        # A run happens unattended; the old implementation called input() here.
        for name in ("a.py", "b.py"):
            (tmp_path / name).write_text("x = 1\n" * 500)

        monkeypatch.setattr("src.agent.analysis.settings.openai_api_key",
                            type("S", (), {"get_secret_value": lambda self: "sk-test"})())

        calls = []

        def fake_llm(prompt_value):
            # The chain is `prompt | llm`, so this receives the rendered prompt.
            calls.append(prompt_value.to_string())
            return type("R", (), {"content": "[]"})()

        # Each file is ~750 estimated tokens, so exactly one fits.
        service = LLMAnalysisService(str(tmp_path), ["a.py", "b.py"], token_limit=800)
        service._llm = RunnableLambda(fake_llm)

        service.run_semantic_analysis()

        # First file fits in the budget, second does not.
        assert len(calls) == 1 and "a.py" in calls[0]
        assert service.tokens_used <= 800
