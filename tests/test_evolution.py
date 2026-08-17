from guanchao.detection import Calibration
from guanchao.domain import FeatureVector
from guanchao.evolution import EvolutionEngine,LabeledExample
from guanchao.policy import PolicyProfile


def make(i,label):
    base=.78 if label else .12
    return LabeledExample(FeatureVector(commercial_language=base,call_to_action=base,contact_pressure=base*.7,template_reuse=base*.7,profile_commerciality=base*.8,authentic_variation=.1 if label else .75),label,f"case-{i}")


def test_evolution_requires_enough_feedback():
    r=EvolutionEngine().evolve(Calibration(),[make(i,i%2) for i in range(6)],PolicyProfile()); assert not r.accepted and r.examples==6


def test_cross_validated_evolution_returns_bounded_report():
    examples=[make(i,i%2) for i in range(30)]; r=EvolutionEngine().evolve(Calibration(),examples,PolicyProfile())
    assert r.examples==30 and -1<=r.worst_fold_delta<=1
    assert .66<=r.policy_profile.challenge_confidence<=.86
