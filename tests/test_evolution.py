from guanchao.detection import Calibration, MarketingDetector
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


class _FeatureProbabilityEvolution(EvolutionEngine):
    @staticmethod
    def _predict(calibration, features):
        return features.commercial_language


def test_operating_point_keeps_distinct_positive_and_high_confidence_bands():
    examples = [
        LabeledExample(FeatureVector(commercial_language=probability), label, f"g-{index}")
        for index, (probability, label) in enumerate(
            [(.10, 0), (.20, 0), (.70, 1), (.80, 1), (.90, 1)]
        )
    ]
    calibration = _FeatureProbabilityEvolution()._select_operating_point(
        Calibration(), examples
    )
    upper_abstain = calibration.decision_threshold + calibration.abstain_margin

    assert calibration.high_threshold > upper_abstain
    midpoint = (upper_abstain + calibration.high_threshold) / 2.0
    assert MarketingDetector(calibration)._label(midpoint, 1.0, 1.0) == "明显营销倾向"
