from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable

from .detection import Calibration
from .domain import FeatureVector


@dataclass(slots=True)
class LabeledExample:
    features: FeatureVector
    label: int


@dataclass(slots=True)
class EvolutionReport:
    accepted: bool
    baseline_score: float
    candidate_score: float
    examples: int
    reason: str
    calibration: Calibration

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "baseline_score": self.baseline_score,
            "candidate_score": self.candidate_score,
            "examples": self.examples,
            "reason": self.reason,
            "calibration": self.calibration.to_dict(),
        }


class EvolutionEngine:
    """Regression-gated calibration evolution. Source code is never self-modified."""

    def evolve(self, current: Calibration, examples: list[LabeledExample]) -> EvolutionReport:
        if len(examples) < 8:
            return EvolutionReport(False, 0.0, 0.0, len(examples), "至少需要 8 条人工复核记录", current)
        train, holdout = self._split(examples)
        if len(holdout) < 2 or len({x.label for x in holdout}) < 2:
            return EvolutionReport(False, 0.0, 0.0, len(examples), "留出样本需要同时包含两类复核结果", current)

        baseline = self._metric(current, holdout)
        candidates = [
            self._gradient_candidate(current, train, step=0.12),
            self._gradient_candidate(current, train, step=0.22),
            self._gradient_candidate(current, train, step=0.34),
        ]
        scored = [(self._metric(candidate, holdout), candidate) for candidate in candidates]
        candidate_score, best = max(scored, key=lambda item: item[0])
        accepted = candidate_score >= baseline + 0.006 and self._no_class_regression(current, best, holdout)
        if accepted:
            return EvolutionReport(True, baseline, candidate_score, len(examples), "候选校准在留出复核上稳定提升，已接纳", best)
        return EvolutionReport(False, baseline, candidate_score, len(examples), "候选校准没有通过留出回放与回归门槛", current)

    @staticmethod
    def _split(examples: list[LabeledExample]) -> tuple[list[LabeledExample], list[LabeledExample]]:
        train: list[LabeledExample] = []
        holdout: list[LabeledExample] = []
        for item in examples:
            fingerprint = "|".join(f"{v:.5f}" for v in item.features.asdict().values()) + f"|{item.label}"
            bucket = int(hashlib.sha256(fingerprint.encode()).hexdigest()[:8], 16) % 5
            (holdout if bucket == 0 else train).append(item)
        if not holdout:
            holdout = examples[-max(2, len(examples) // 5) :]
            train = examples[: -len(holdout)]
        return train, holdout

    def _gradient_candidate(self, current: Calibration, examples: Iterable[LabeledExample], step: float) -> Calibration:
        examples = list(examples)
        if not examples:
            return current
        grad_bias = 0.0
        grad = {key: 0.0 for key in current.weights}
        for item in examples:
            p = self._predict(current, item.features)
            error = p - item.label
            grad_bias += error
            for key in grad:
                grad[key] += error * getattr(item.features, key)
        scale = 1.0 / len(examples)
        bias = _clip(current.bias - step * grad_bias * scale, -4.5, 1.0)
        weights = {}
        for key, old in current.weights.items():
            proposed = old - step * grad[key] * scale
            if key == "authentic_variation":
                proposed = min(-0.05, proposed)
            else:
                proposed = max(0.02, proposed)
            weights[key] = _clip(proposed, -2.5, 2.5)
        return Calibration(bias=bias, weights=weights)

    def _metric(self, calibration: Calibration, examples: list[LabeledExample]) -> float:
        probs = [self._predict(calibration, item.features) for item in examples]
        labels = [item.label for item in examples]
        preds = [1 if p >= 0.5 else 0 for p in probs]
        tpr = self._recall(preds, labels, 1)
        tnr = self._recall(preds, labels, 0)
        balanced = (tpr + tnr) / 2
        brier = sum((p - y) ** 2 for p, y in zip(probs, labels)) / len(labels)
        return balanced - 0.28 * brier

    def _no_class_regression(self, old: Calibration, new: Calibration, examples: list[LabeledExample]) -> bool:
        labels = [e.label for e in examples]
        old_preds = [1 if self._predict(old, e.features) >= 0.5 else 0 for e in examples]
        new_preds = [1 if self._predict(new, e.features) >= 0.5 else 0 for e in examples]
        for klass in (0, 1):
            if self._recall(new_preds, labels, klass) + 0.06 < self._recall(old_preds, labels, klass):
                return False
        return True

    @staticmethod
    def _recall(preds: list[int], labels: list[int], klass: int) -> float:
        idx = [i for i, y in enumerate(labels) if y == klass]
        if not idx:
            return 0.5
        return sum(1 for i in idx if preds[i] == klass) / len(idx)

    @staticmethod
    def _predict(calibration: Calibration, features: FeatureVector) -> float:
        linear = calibration.bias + sum(calibration.weights[k] * getattr(features, k) for k in calibration.weights)
        linear = max(-20.0, min(20.0, linear))
        return 1.0 / (1.0 + math.exp(-linear))


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
