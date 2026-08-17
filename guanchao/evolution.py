from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass

from .detection import Calibration
from .detection_support import _clip_range, interaction_value
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
    """Stratified cross-fit calibration with trust-region and regression gates."""

    def evolve(
        self,
        current: Calibration,
        examples: list[LabeledExample],
        policy: PolicyProfile | None = None,
    ) -> EvolutionReport:
        policy = policy or PolicyProfile()
        if len(examples) < 10:
            return self._reject(current, policy, examples, "至少需要 10 条人工复核记录")
        if len({x.label for x in examples}) < 2:
            return self._reject(current, policy, examples, "复核记录需要同时包含两类结果")

        folds = self._folds(examples, 5)
        baselines: list[float] = []
        candidates: list[float] = []
        deltas: list[float] = []
        fitted: list[Calibration] = []
        for index, holdout in enumerate(folds):
            train = [x for j, fold in enumerate(folds) if j != index for x in fold]
            if not holdout or len({x.label for x in holdout}) < 2:
                continue
            baseline = self._metric(current, holdout)
            local = [self._fit(current, train, step, epochs) for step, epochs in ((.035, 28), (.055, 36), (.08, 44))]
            scored = [(self._metric(candidate, holdout), candidate) for candidate in local]
            score, best = max(scored, key=lambda row: row[0])
            baselines.append(baseline)
            candidates.append(score)
            deltas.append(score - baseline)
            fitted.append(best)
        if len(deltas) < 2:
            return self._reject(current, policy, examples, "有效回放折数不足")

        best = self._aggregate(fitted, current)
        baseline = statistics.fmean(baselines)
        candidate = statistics.fmean(candidates)
        worst = min(deltas)
        accepted = candidate >= baseline + .004 and worst >= -.015 and self._no_class_regression(current, best, examples)
        profile = self._policy_candidate(policy, best, examples) if accepted else policy
        reason = "候选通过分层多折回放、最差折与类别回归门槛" if accepted else "候选没有同时通过平均提升、最差折与类别回归门槛"
        return EvolutionReport(accepted, baseline, candidate, worst, len(examples), reason, best if accepted else current, profile)

    def _reject(self, current: Calibration, policy: PolicyProfile, examples: list[LabeledExample], reason: str) -> EvolutionReport:
        return EvolutionReport(False, 0, 0, 0, len(examples), reason, current, policy)

    @staticmethod
    def _folds(examples: list[LabeledExample], k: int) -> list[list[LabeledExample]]:
        folds = [[] for _ in range(k)]
        by_class: dict[int, list[tuple[str, LabeledExample]]] = {0: [], 1: []}
        for item in examples:
            fingerprint = item.group or "|".join(f"{v:.5f}" for v in item.features.asdict().values())
            digest = hashlib.sha256(f"{item.label}|{fingerprint}".encode()).hexdigest()
            by_class.setdefault(item.label, []).append((digest, item))
        for label in sorted(by_class):
            for index, (_, item) in enumerate(sorted(by_class[label], key=lambda row: row[0])):
                folds[index % k].append(item)
        return folds

    def _fit(self, current: Calibration, examples: list[LabeledExample], step: float, epochs: int) -> Calibration:
        if not examples:
            return current
        bias = current.bias
        weights = dict(current.weights)
        interactions = dict(current.interactions)
        anchor_w, anchor_i, anchor_b = dict(weights), dict(interactions), bias
        reg = .055
        for epoch in range(epochs):
            gb = 0.0
            gw = {key: 0.0 for key in weights}
            gi = {key: 0.0 for key in interactions}
            probe = Calibration(
                bias=bias, weights=weights, interactions=interactions,
                temperature=current.temperature, semantic_weight=current.semantic_weight,
                decision_threshold=current.decision_threshold, abstain_margin=current.abstain_margin,
                high_threshold=current.high_threshold,
            )
            for item in examples:
                error = self._predict(probe, item.features) - item.label
                gb += error
                for key in gw:
                    gw[key] += error * getattr(item.features, key, 0.0)
                for key in gi:
                    gi[key] += error * interaction_value(item.features, key)
            scale = 1 / len(examples)
            lr = step / math.sqrt(1 + epoch / 8)
            bias = _clip_range(bias - lr * (gb * scale + reg * (bias - anchor_b)), -4.5, 1.0)
            for key in weights:
                value = weights[key] - lr * (gw[key] * scale + reg * (weights[key] - anchor_w[key]))
                if key == "authentic_variation": value = min(-.03, value)
                elif key != "disclosure_signal": value = max(.01, value)
                weights[key] = _clip_range(value, -2.5, 2.5)
            for key in interactions:
                value = interactions[key] - lr * (gi[key] * scale + reg * (interactions[key] - anchor_i[key]))
                if key == "commercial_authentic": value = min(-.02, value)
                else: value = max(0.0, value)
                interactions[key] = _clip_range(value, -1.8, 1.8)
        return self._tune(Calibration(
            bias=bias, weights=weights, interactions=interactions,
            temperature=current.temperature, semantic_weight=current.semantic_weight,
            decision_threshold=current.decision_threshold, abstain_margin=current.abstain_margin,
            high_threshold=current.high_threshold,
        ), examples)

    def _tune(self, candidate: Calibration, examples: list[LabeledExample]) -> Calibration:
        scored: list[tuple[float, float, float, float]] = []
        temperatures = {_clip_range(candidate.temperature * factor, .62, 1.65) for factor in (.82, .92, 1, 1.10, 1.22)}
        thresholds = {_clip_range(candidate.decision_threshold + offset, .34, .66) for offset in (-.08, -.04, 0, .04, .08)}
        margins = {_clip_range(candidate.abstain_margin * factor, .03, .18) for factor in (.70, .88, 1, 1.18, 1.40)}
        for temperature in temperatures:
            for threshold in thresholds:
                for margin in margins:
                    probe = Calibration(
                        bias=candidate.bias, weights=dict(candidate.weights), interactions=dict(candidate.interactions),
                        temperature=temperature, semantic_weight=candidate.semantic_weight,
                        decision_threshold=threshold, abstain_margin=margin,
                        high_threshold=max(threshold + max(.12, margin), candidate.high_threshold),
                    )
                    scored.append((self._metric(probe, examples), temperature, threshold, margin))
        _, temperature, threshold, margin = max(scored, key=lambda row: row[0])
        return Calibration(
            bias=candidate.bias, weights=dict(candidate.weights), interactions=dict(candidate.interactions),
            temperature=temperature, semantic_weight=candidate.semantic_weight,
            decision_threshold=threshold, abstain_margin=margin,
            high_threshold=max(threshold + max(.12, margin), candidate.high_threshold),
        )

    @staticmethod
    def _aggregate(candidates: list[Calibration], fallback: Calibration) -> Calibration:
        if not candidates:
            return fallback
        med = statistics.median
        return Calibration(
            bias=med([c.bias for c in candidates]),
            weights={key: med([c.weights[key] for c in candidates]) for key in fallback.weights},
            interactions={key: med([c.interactions[key] for c in candidates]) for key in fallback.interactions},
            temperature=med([c.temperature for c in candidates]),
            semantic_weight=fallback.semantic_weight,
            decision_threshold=med([c.decision_threshold for c in candidates]),
            abstain_margin=med([c.abstain_margin for c in candidates]),
            high_threshold=med([c.high_threshold for c in candidates]),
        )

    def _metric(self, calibration: Calibration, examples: list[LabeledExample]) -> float:
        probs = [self._predict(calibration, x.features) for x in examples]
        labels = [x.label for x in examples]
        preds = [int(p >= calibration.decision_threshold) for p in probs]
        balanced = (self._recall(preds, labels, 1) + self._recall(preds, labels, 0)) / 2
        brier = statistics.fmean((p - y) ** 2 for p, y in zip(probs, labels))
        ece = self._ece(probs, labels)
        kept = [i for i, p in enumerate(probs) if abs(p - calibration.decision_threshold) >= calibration.abstain_margin]
        selective_error = statistics.fmean(preds[i] != labels[i] for i in kept) if kept else 1.0
        coverage = len(kept) / len(labels)
        return balanced - .24 * brier - .08 * ece - .10 * selective_error - .03 * (1 - coverage)

    def _no_class_regression(self, old: Calibration, new: Calibration, examples: list[LabeledExample]) -> bool:
        labels = [x.label for x in examples]
        oldp = [int(self._predict(old, x.features) >= old.decision_threshold) for x in examples]
        newp = [int(self._predict(new, x.features) >= new.decision_threshold) for x in examples]
        return all(self._recall(newp, labels, klass) + .04 >= self._recall(oldp, labels, klass) for klass in (0, 1))

    def _policy_candidate(self, profile: PolicyProfile, new: Calibration, examples: list[LabeledExample]) -> PolicyProfile:
        probs = [self._predict(new, x.features) for x in examples]
        false_pos = sum(x.label == 0 and p >= new.decision_threshold for x, p in zip(examples, probs))
        false_neg = sum(x.label == 1 and p < new.decision_threshold for x, p in zip(examples, probs))
        pressure = (false_pos - false_neg) / max(1, len(examples))
        return PolicyProfile(
            challenge_confidence=_clip_range(profile.challenge_confidence + pressure * .08, .66, .86),
            stability_confidence=_clip_range(profile.stability_confidence + abs(pressure) * .04, .72, .88),
            min_pattern_posts=profile.min_pattern_posts,
            min_stability_posts=profile.min_stability_posts,
            verdict_evidence_floor=3 if false_pos / max(1, len(examples)) > .16 else 2,
            cost_weight=profile.cost_weight,
            caution_gain=profile.caution_gain,
        )

    @staticmethod
    def _recall(preds: list[int], labels: list[int], klass: int) -> float:
        indexes = [i for i, label in enumerate(labels) if label == klass]
        return .5 if not indexes else sum(preds[i] == klass for i in indexes) / len(indexes)

    @staticmethod
    def _ece(probs: list[float], labels: list[int], bins: int = 5) -> float:
        error = 0.0
        for index in range(bins):
            low, high = index / bins, (index + 1) / bins
            rows = [(p, y) for p, y in zip(probs, labels) if low <= p < high or (index == bins - 1 and p == 1)]
            if rows:
                error += len(rows) / len(probs) * abs(statistics.fmean(p for p, _ in rows) - statistics.fmean(y for _, y in rows))
        return error

    @staticmethod
    def _predict(calibration: Calibration, features: FeatureVector) -> float:
        linear = calibration.bias
        linear += sum(calibration.weights[key] * getattr(features, key, 0.0) for key in calibration.weights)
        linear += sum(calibration.interactions[key] * interaction_value(features, key) for key in calibration.interactions)
        linear /= max(.55, calibration.temperature)
        return 1 / (1 + math.exp(-max(-20, min(20, linear))))
