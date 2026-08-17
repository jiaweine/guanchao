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


def test_evolution_requires_enough_reviews():
    report = EvolutionEngine().evolve(
        Calibration(), [make(i, i % 2) for i in range(6)], PolicyProfile()
    )
    assert not report.accepted and report.examples == 6


def test_cross_validated_evolution_returns_bounded_report():
    examples = [make(i, i % 2) for i in range(30)]
    report = EvolutionEngine().evolve(Calibration(), examples, PolicyProfile())
    assert report.examples == 30 and -1 <= report.worst_fold_delta <= 1
    assert .66 <= report.policy_profile.challenge_confidence <= .86
