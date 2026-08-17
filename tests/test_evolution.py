from guanchao.detection import Calibration
from guanchao.domain import FeatureVector
from guanchao.evolution import EvolutionEngine, LabeledExample
from guanchao.policy import PolicyProfile


def make(i, label):
    base = .78 if label else .12
    return LabeledExample(
        FeatureVector(
            commercial_language=base,
            call_to_action=base,
            contact_pressure=base * .7,
            template_reuse=base * .7,
            profile_commerciality=base * .8,
            authentic_variation=.1 if label else .75,
        ),
        label,
        f"case-{i}",
    )


def test_evolution_requires_both_review_classes():
    report = EvolutionEngine().evolve(
        Calibration(), [make(i, 1) for i in range(8)], PolicyProfile()
    )
    assert not report.accepted and report.examples == 8
    assert "双类" in report.reason


def test_cross_validated_evolution_uses_zero_regression_gate():
    examples = [make(i, i % 2) for i in range(30)]
    profile = PolicyProfile()
    report = EvolutionEngine().evolve(Calibration(), examples, profile)
    assert report.examples == 30 and -1 <= report.worst_fold_delta <= 1
    assert report.policy_profile == profile
    assert not hasattr(report.policy_profile, "challenge_confidence")
    assert set(report.policy_profile.weights) >= {
        "stability.probe",
        "evidence.challenge",
        "verdict.compose",
    }
    if report.accepted:
        assert report.worst_fold_delta >= 0
        assert report.candidate_score > report.baseline_score
        assert 0 < report.calibration.decision_threshold < 1
        assert 0 <= report.calibration.abstain_margin < .5
