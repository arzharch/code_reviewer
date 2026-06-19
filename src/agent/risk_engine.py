from src.agent.state import RiskAssessment, TestResult, Proposal
from typing import Literal

class RiskEngine:
    def __init__(self, auto_commit_threshold: float = 0.15):
        self.auto_commit_threshold = auto_commit_threshold

    def calculate_risk(
        self,
        proposal: Proposal,
        test_result: TestResult,
        file_criticality_score: float,
        blast_radius_normalized: float,
        static_analysis_severity_normalized: float,
        historical_revert_rate: float,
        semantic_risk_flag: float
    ) -> RiskAssessment:
        """
        Calculates the risk of an automated commit deterministically.
        Hard gates: failed test or high-risk file criticality forces escalation.
        """
        # 1. Hard Gate: Did the tests pass?
        if not test_result.passed:
            return RiskAssessment(
                proposal_id=proposal.finding_id,
                score=1.0,
                signals={"test_failed": 1.0},
                decision="escalate",
                reasons=["Test suite failed after applying this proposal."]
            )
            
        # 2. Hard Gate: File criticality
        if file_criticality_score >= 1.0:
            return RiskAssessment(
                proposal_id=proposal.finding_id,
                score=1.0,
                signals={"file_criticality": file_criticality_score},
                decision="escalate",
                reasons=["Modification touches a highly critical or protected file path."]
            )

        # 3. Weighted Score Calculation
        w_file = 0.2
        w_blast = 0.2
        w_static = 0.1
        w_conf = 0.1
        w_hist = 0.2
        w_sem = 0.2

        model_uncertainty = max(0.0, 1.0 - proposal.confidence)

        risk_score = (
            w_file * file_criticality_score +
            w_blast * blast_radius_normalized +
            w_static * static_analysis_severity_normalized +
            w_conf * model_uncertainty +
            w_hist * historical_revert_rate +
            w_sem * semantic_risk_flag
        )

        signals = {
            "file_criticality": file_criticality_score,
            "blast_radius": blast_radius_normalized,
            "static_severity": static_analysis_severity_normalized,
            "model_uncertainty": model_uncertainty,
            "historical_revert_rate": historical_revert_rate,
            "semantic_risk": semantic_risk_flag
        }

        reasons = []
        decision: Literal["auto_commit", "escalate"]
        if risk_score > self.auto_commit_threshold:
            decision = "escalate"
            reasons.append(f"Risk score ({risk_score:.3f}) exceeds auto-commit threshold ({self.auto_commit_threshold}).")
        else:
            decision = "auto_commit"
            reasons.append("Proposal passed all risk checks and is safe to auto-commit.")

        return RiskAssessment(
            proposal_id=proposal.finding_id,
            score=risk_score,
            signals=signals,
            decision=decision,
            reasons=reasons
        )
