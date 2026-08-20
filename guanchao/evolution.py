from __future__ import annotations

import hashlib
import math
import os
import statistics
from dataclasses import dataclass

from .detection import Calibration
from .detection_support import interaction_value
from .domain import FeatureVector
from .policy import PolicyProfile


@dataclass(slots=True)
class LabeledExample:
    features: FeatureVector
    label: int
    group: str = ""


@dataclass(slots=True)
class EvolutionReport:
    accepted: bool
    baseline_score: float
    candidate_score: float
    worst_fold_delta: float
    examples: int
    reason: str
    calibration: Calibration
    policy_profile: PolicyProfile

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "baseline_score": self.baseline_score,
            "candidate_score": self.candidate_score,
            "worst_fold_delta": self.worst_fold_delta,
            "examples": self.examples,
            "reason": self.reason,
            "calibration": self.calibration.to_dict(),
            "policy_profile": self.policy_profile.to_dict(),
        }


class EvolutionEngine:
    """Data-adaptive cross-fit calibration with zero-regression promotion."""

    def evolve(
        self,
        current: Calibration,
        examples: list[LabeledExample],
        policy: PolicyProfile | None = None,
    ) -> EvolutionReport:
        policy = policy or PolicyProfile()
        counts = {label: sum(item.label == label for item in examples) for label in (0, 1)}
        if min(counts.values(), default=0) < 2:
            return self._reject(current, policy, examples, "复核样本尚不足以形成双类交叉回放")
        max_folds = max(2, int(os.getenv("GUANCHAO_EVOLUTION_MAX_FOLDS", "5")))
        k = max(2, min(max_folds, min(counts.values()), int(math.sqrt(len(examples))) or 2))
        folds = self._folds(examples, k)
        baselines: list[float] = []
        candidates: list[float] = []
        deltas: list[float] = []
        fitted: list[Calibration] = []
        for index, holdout in enumerate(folds):
            train = [item for fold_index, fold in enumerate(folds) if fold_index != index for item in fold]
            if not holdout or len({item.label for item in holdout}) < 2:
                continue
            baseline = self._metric(current, holdout)
            candidate = self._fit(current, train)
            score = self._metric(candidate, holdout)
            baselines.append(baseline)
            candidates.append(score)
            deltas.append(score - baseline)
            fitted.append(candidate)
        if len(deltas) < 2:
            return self._reject(current, policy, examples, "有效交叉回放折数不足")
        best = self._aggregate(fitted, current)
        baseline_score = statistics.fmean(baselines)
        candidate_score = statistics.fmean(candidates)
        worst = min(deltas)
        accepted = (
            statistics.median(deltas) > 0.0
            and worst >= 0.0
            and self._metric(best, examples) > self._metric(current, examples)
            and self._no_class_regression(current, best, examples)
        )
        reason = (
            "候选在所有有效回放折上均未退化，并提升整体风险覆盖表现"
            if accepted
            else "候选没有同时通过逐折零退化、整体提升与双类召回保护"
        )
        return EvolutionReport(
            accepted,
            baseline_score,
            candidate_score,
            worst,
            len(examples),
            reason,
            best if accepted else current,
            policy,
        )

    def _reject(
        self,
        current: Calibration,
        policy: PolicyProfile,
        examples: list[LabeledExample],
        reason: str,
    ) -> EvolutionReport:
        return EvolutionReport(False, 0.0, 0.0, 0.0, len(examples), reason, current, policy)

    @staticmethod
    def _folds(examples: list[LabeledExample], k: int) -> list[list[LabeledExample]]:
        folds = [[] for _ in range(k)]
        by_class: dict[int, list[tuple[str, LabeledExample]]] = {0: [], 1: []}
        for item in examples:
            fingerprint = item.group or "|".join(f"{value:.6f}" for value in item.features.asdict().values())
            digest = hashlib.sha256(f"{item.label}|{fingerprint}".encode()).hexdigest()
            by_class.setdefault(item.label, []).append((digest, item))
        for label in sorted(by_class):
            for index, (_, item) in enumerate(sorted(by_class[label], key=lambda row: row[0])):
                folds[index % k].append(item)
        return folds

    def _fit(self, current: Calibration, examples: list[LabeledExample]) -> Calibration:
        if not examples:
            return current
        names = ["__bias__", *current.weights.keys(), *current.interactions.keys()]
        params = {"__bias__": current.bias, **dict(current.weights), **dict(current.interactions)}
        anchors = dict(params)
        regularization = 1.0 / math.sqrt(len(examples))
        temperature = max(1e-6, current.temperature)
        sample_count = len(examples)
        for _ in range(50):
            gradients = {name: 0.0 for name in names}
            curvature = {name: regularization * sample_count for name in names}
            for item in examples:
                values = self._design(item.features, current)
                linear = sum(params[name] * values[name] for name in names) / temperature
                probability = 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, linear))))
                error = probability - item.label
                variance = probability * (1.0 - probability)
                for name in names:
                    scaled_value = values[name] / temperature
                    gradients[name] += error * scaled_value
                    curvature[name] += variance * scaled_value * scaled_value
            max_change = 0.0
            scale = 1.0 / sample_count
            for name in names:
                gradient = gradients[name] * scale + regularization * (params[name] - anchors[name])
                hessian = curvature[name] * scale
                change = gradient / max(hessian, 1e-9)
                params[name] -= change
                max_change = max(max_change, abs(change))
            if max_change <= 1e-7:
                break
        candidate = Calibration(
            bias=params["__bias__"],
            weights={name: params[name] for name in current.weights},
            interactions={name: params[name] for name in current.interactions},
            temperature=current.temperature,
            semantic_weight=current.semantic_weight,
            decision_threshold=current.decision_threshold,
            abstain_margin=current.abstain_margin,
            high_threshold=current.high_threshold,
        )
        return self._select_operating_point(candidate, examples)

    def _select_operating_point(
        self, candidate: Calibration, examples: list[LabeledExample]
    ) -> Calibration:
        probabilities = [self._predict(candidate, item.features) for item in examples]
        ordered = sorted(set(probabilities))
        thresholds = [candidate.decision_threshold]
        thresholds.extend((left + right) / 2.0 for left, right in zip(ordered, ordered[1:]))
        if ordered:
            thresholds.extend((ordered[0], ordered[-1]))
        labels = [item.label for item in examples]
        best_threshold = candidate.decision_threshold
        best_score = -1.0
        for threshold in thresholds:
            predictions = [int(probability >= threshold) for probability in probabilities]
            score = math.sqrt(
                self._recall(predictions, labels, 0) * self._recall(predictions, labels, 1)
            )
            if score > best_score:
                best_score = score
                best_threshold = threshold
        distances = sorted(set(abs(probability - best_threshold) for probability in probabilities))
        predictions = [int(probability >= best_threshold) for probability in probabilities]
        best_margin = 0.0
        best_selective = -1.0
        for margin in [0.0, *distances]:
            kept = [
                index
                for index, probability in enumerate(probabilities)
                if abs(probability - best_threshold) >= margin
            ]
            if not kept:
                continue
            accuracy = sum(predictions[index] == labels[index] for index in kept) / len(kept)
            coverage = len(kept) / len(labels)
            score = accuracy * math.sqrt(coverage)
            if score > best_selective:
                best_selective = score
                best_margin = margin

        # The high-confidence band must remain distinct from the ordinary positive
        # band. Using threshold + abstain_margin directly makes every positive
        # score outside the abstain band immediately "高度营销化" and collapses the
        # intermediate "明显营销倾向" state after an evolution promotion.
        upper_abstain = min(1.0, best_threshold + best_margin)
        strong_positive = [
            probability
            for probability, label in zip(probabilities, labels)
            if label == 1 and probability > upper_abstain
        ]
        high_threshold = (
            statistics.median(strong_positive)
            if strong_positive
            else max(candidate.high_threshold, upper_abstain)
        )
        high_threshold = min(1.0, max(upper_abstain, high_threshold))

        return Calibration(
            bias=candidate.bias,
            weights=dict(candidate.weights),
            interactions=dict(candidate.interactions),
            temperature=candidate.temperature,
            semantic_weight=candidate.semantic_weight,
            decision_threshold=best_threshold,
            abstain_margin=best_margin,
            high_threshold=high_threshold,
        )

    def _metric(self, calibration: Calibration, examples: list[LabeledExample]) -> float:
        if not examples:
            return 0.0
        probabilities = [self._predict(calibration, item.features) for item in examples]
        labels = [item.label for item in examples]
        predictions = [
            int(probability >= calibration.decision_threshold) for probability in probabilities
        ]
        balanced = math.sqrt(
            self._recall(predictions, labels, 0) * self._recall(predictions, labels, 1)
        )
        brier = statistics.fmean(
            (probability - label) ** 2 for probability, label in zip(probabilities, labels)
        )
        ece = self._ece(probabilities, labels)
        kept = [
            index
            for index, probability in enumerate(probabilities)
            if abs(probability - calibration.decision_threshold) >= calibration.abstain_margin
        ]
        if not kept:
            return 0.0
        selective_accuracy = sum(predictions[index] == labels[index] for index in kept) / len(kept)
        coverage = len(kept) / len(labels)
        return (
            balanced
            * (1.0 - brier)
            * (1.0 - ece)
            * math.sqrt(selective_accuracy * coverage)
        )

    def _no_class_regression(
        self, old: Calibration, new: Calibration, examples: list[LabeledExample]
    ) -> bool:
        labels = [item.label for item in examples]
        old_predictions = [
            int(self._predict(old, item.features) >= old.decision_threshold) for item in examples
        ]
        new_predictions = [
            int(self._predict(new, item.features) >= new.decision_threshold) for item in examples
        ]
        return all(
            self._recall(new_predictions, labels, klass)
            >= self._recall(old_predictions, labels, klass)
            for klass in (0, 1)
        )

    @staticmethod
    def _aggregate(candidates: list[Calibration], fallback: Calibration) -> Calibration:
        if not candidates:
            return fallback
        median = statistics.median
        return Calibration(
            bias=median([candidate.bias for candidate in candidates]),
            weights={
                key: median([candidate.weights[key] for candidate in candidates])
                for key in fallback.weights
            },
            interactions={
                key: median([candidate.interactions[key] for candidate in candidates])
                for key in fallback.interactions
            },
            temperature=median([candidate.temperature for candidate in candidates]),
            semantic_weight=fallback.semantic_weight,
            decision_threshold=median([candidate.decision_threshold for candidate in candidates]),
            abstain_margin=median([candidate.abstain_margin for candidate in candidates]),
            high_threshold=median([candidate.high_threshold for candidate in candidates]),
        )

    @staticmethod
    def _design(features: FeatureVector, calibration: Calibration) -> dict[str, float]:
        return {
            "__bias__": 1.0,
            **{key: float(getattr(features, key, 0.0)) for key in calibration.weights},
            **{
                key: interaction_value(features, key)
                for key in calibration.interactions
            },
        }

    @staticmethod
    def _recall(predictions: list[int], labels: list[int], klass: int) -> float:
        indexes = [index for index, label in enumerate(labels) if label == klass]
        if not indexes:
            return 0.0
        return sum(predictions[index] == klass for index in indexes) / len(indexes)

    @staticmethod
    def _ece(probabilities: list[float], labels: list[int]) -> float:
        bins = max(2, int(math.sqrt(len(probabilities))))
        error = 0.0
        for index in range(bins):
            low, high = index / bins, (index + 1) / bins
            rows = [
                (probability, label)
                for probability, label in zip(probabilities, labels)
                if low <= probability < high or (index == bins - 1 and probability == 1.0)
            ]
            if rows:
                error += len(rows) / len(probabilities) * abs(
                    statistics.fmean(probability for probability, _ in rows)
                    - statistics.fmean(label for _, label in rows)
                )
        return error

    @staticmethod
    def _predict(calibration: Calibration, features: FeatureVector) -> float:
        linear = calibration.bias
        linear += sum(
            calibration.weights[key] * getattr(features, key, 0.0)
            for key in calibration.weights
        )
        linear += sum(
            calibration.interactions[key] * interaction_value(features, key)
            for key in calibration.interactions
        )
        linear /= max(1e-6, calibration.temperature)
        return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, linear))))
