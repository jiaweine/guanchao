from __future__ import annotations

from dataclasses import dataclass, field

from .detection_support import _clip_range


@dataclass(slots=True)
class Calibration:
    """Learnable verdict parameters with a cold-start prior.

    The defaults only provide an initial operating point. Once decisive review
    data exists, the Harness can promote data-adaptive candidates through the
    replay gate; they are not immutable business rules.
    """

    bias: float = -0.70
    weights: dict[str, float] = field(default_factory=lambda: {
        "commercial_language": 1.12,
        "call_to_action": 1.02,
        "contact_pressure": 0.92,
        "template_reuse": 0.92,
        "cadence_burst": 0.52,
        "engagement_pattern": 0.42,
        "profile_commerciality": 0.72,
        "cross_post_pressure": 0.78,
        "disclosure_signal": 0.08,
        "authentic_variation": -0.96,
        "media_commerciality": 0.78,
        "identity_consistency": 0.58,
    })
    interactions: dict[str, float] = field(default_factory=lambda: {
        "intent_action": 0.72,
        "action_contact": 0.58,
        "profile_conversion": 0.48,
        "template_cadence": 0.32,
        "media_identity": 0.42,
        "commercial_authentic": -0.44,
        "action_persistence": 0.62,
        "contact_persistence": 0.52,
    })
    temperature: float = 1.0
    semantic_weight: float = 0.52
    decision_threshold: float = 0.50
    abstain_margin: float = 0.06
    high_threshold: float = 0.78

    def to_dict(self) -> dict[str, object]:
        return {
            "bias": self.bias,
            "weights": dict(self.weights),
            "interactions": dict(self.interactions),
            "temperature": self.temperature,
            "semantic_weight": self.semantic_weight,
            "decision_threshold": self.decision_threshold,
            "abstain_margin": self.abstain_margin,
            "high_threshold": self.high_threshold,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object] | None) -> "Calibration":
        base = cls()
        if not raw:
            return base
        bias = float(raw.get("bias", base.bias))
        weights = dict(base.weights)
        incoming = raw.get("weights")
        if isinstance(incoming, dict):
            for key in weights:
                if key in incoming:
                    weights[key] = float(incoming[key])
        interactions = dict(base.interactions)
        incoming_interactions = raw.get("interactions")
        if isinstance(incoming_interactions, dict):
            for key in interactions:
                if key in incoming_interactions:
                    interactions[key] = float(incoming_interactions[key])
        temperature = _clip_range(float(raw.get("temperature", base.temperature)), 0.1, 10.0)
        semantic_weight = _clip_range(float(raw.get("semantic_weight", base.semantic_weight)), 0.0, 1.0)
        decision_threshold = _clip_range(float(raw.get("decision_threshold", base.decision_threshold)), 0.01, 0.99)
        abstain_margin = _clip_range(float(raw.get("abstain_margin", base.abstain_margin)), 0.0, 0.49)
        maximum_band = max(0.0, min(decision_threshold, 1.0 - decision_threshold))
        effective_abstain = min(maximum_band, abstain_margin)
        high_floor = min(1.0, decision_threshold + effective_abstain)
        high_threshold = _clip_range(float(raw.get("high_threshold", base.high_threshold)), high_floor, 1.0)
        return cls(
            bias=bias,
            weights=weights,
            interactions=interactions,
            temperature=temperature,
            semantic_weight=semantic_weight,
            decision_threshold=decision_threshold,
            abstain_margin=abstain_margin,
            high_threshold=high_threshold,
        )
