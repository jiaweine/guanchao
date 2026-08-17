from guanchao.detection import Calibration
from guanchao.domain import FeatureVector
from guanchao.evolution import EvolutionEngine, LabeledExample


def fv(base: float, authentic: float = 0.05) -> FeatureVector:
    return FeatureVector(
        commercial_language=base,
        call_to_action=base,
        contact_pressure=max(0, base - 0.15),
        template_reuse=base,
        cadence_burst=base * 0.7,
        engagement_pattern=base * 0.4,
        profile_commerciality=base * 0.8,
        cross_post_pressure=base,
        disclosure_signal=base * 0.3,
        authentic_variation=authentic,
    )


def test_evolution_is_gated_and_bounded():
    examples = []
    for i in range(14):
        examples.append(LabeledExample(fv(0.75 + (i % 3) * 0.04), 1))
        examples.append(LabeledExample(fv(0.08 + (i % 3) * 0.03, authentic=0.75), 0))
    current = Calibration(bias=-0.4)
    report = EvolutionEngine().evolve(current, examples)
    assert report.examples == len(examples)
    assert -4.5 <= report.calibration.bias <= 1.0
    assert report.candidate_score >= report.baseline_score or not report.accepted


def test_evolution_refuses_too_little_feedback():
    report = EvolutionEngine().evolve(Calibration(), [LabeledExample(fv(0.8), 1)] * 3)
    assert not report.accepted
