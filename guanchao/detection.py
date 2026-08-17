from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics

from .domain import AccountSnapshot, DetectionResult, Evidence, FeatureVector, PostSnapshot
from .semantic import SemanticEvidenceGateway, SemanticSignal

from .detection_support import (
    _AUTHENTIC_RE,
    _COMMERCIAL_RE,
    _CONTACT_RE,
    _CTA_RE,
    _DISCLOSURE_RE,
    _INTERACTIONS,
    _OPERATION_RE,
    _PRICE_RE,
    _URL_RE,
    _clamp,
    _clip_range,
    _dedupe,
    _hit,
    _repeated_phrase_pressure,
    _rms,
    _robust_rate,
    _shingles,
    _tokens,
    interaction_value,
)

from .detection_calibration import Calibration
from .detection_behavior import DetectionBehaviorMixin


class MarketingDetector(DetectionBehaviorMixin):
    """Owned evidence detector with optional grounded semantic teacher.

    The semantic model can strengthen feature evidence only when its quotes are
    verifiable in the supplied material. It never supplies the final verdict.
    """

    def __init__(
        self,
        calibration: Calibration | None = None,
        semantic_gateway: SemanticEvidenceGateway | None = None,
    ):
        self.calibration = calibration or Calibration()
        self.semantic_gateway = semantic_gateway
        self._analysis_cache: dict[str, DetectionResult] = {}
        self._stability_cache: dict[str, tuple[float, float]] = {}

    def analyze(self, account: AccountSnapshot, media_text: str = "") -> DetectionResult:
        cache_key = self._cache_key(account, media_text)
        cached = self._analysis_cache.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)
        features, evidence, missing, semantic = self._extract(account, media_text, use_semantic=True)
        marketing = self.score(features)
        stability, swing = self.stability_probe(account, media_text)
        disclosure_gap = max(0.0, 1.0 - features.disclosure_signal)
        covert = _clamp(
            marketing
            * (0.50 + 0.50 * disclosure_gap)
            * (0.86 + 0.14 * features.contact_pressure)
        )
        confidence = self._confidence(account, features, stability, marketing, semantic.grounded_fraction)
        swing_limit = self._swing_limit(len([p for p in account.posts if p.text.strip()]))
        if swing > swing_limit:
            confidence *= max(0.68, 1.0 - 0.75 * swing)
            missing.append("少数内容对结论影响较大，建议补充更多样本")
            evidence.append(
                Evidence(
                    "stability_swing",
                    "结论对少数内容较敏感",
                    "移除单条高影响内容后，判断发生较明显变化。",
                    _clamp(swing / max(0.01, swing_limit * 1.7)),
                    "context",
                )
            )
        confidence = _clamp(confidence)
        label = self._label(marketing, confidence, stability)
        summary = self._summary(label, confidence, evidence, missing)
        result = DetectionResult(
            marketing,
            covert,
            confidence,
            stability,
            label,
            summary,
            features,
            evidence,
            _dedupe(missing),
        )
        self._analysis_cache[cache_key] = copy.deepcopy(result)
        return result

    def score(self, features: FeatureVector) -> float:
        linear = self.calibration.bias
        for key, weight in self.calibration.weights.items():
            linear += weight * float(getattr(features, key, 0.0))
        for key, weight in self.calibration.interactions.items():
            pair = _INTERACTIONS.get(key)
            if not pair:
                continue
            a = max(0.0, float(getattr(features, pair[0], 0.0)))
            b = max(0.0, float(getattr(features, pair[1], 0.0)))
            linear += weight * a * b
        scaled = linear / max(0.55, self.calibration.temperature)
        return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, scaled))))

    def extract(self, account: AccountSnapshot, media_text: str = "") -> tuple[FeatureVector, list[Evidence], list[str]]:
        features, evidence, missing, _ = self._extract(account, media_text, use_semantic=True)
        return features, evidence, missing

    def _extract(
        self,
        account: AccountSnapshot,
        media_text: str = "",
        *,
        use_semantic: bool,
    ) -> tuple[FeatureVector, list[Evidence], list[str], SemanticSignal]:
        posts = [p for p in account.posts if p.text.strip()]
        texts = [p.text for p in posts]
        bio = account.bio
        commercial = _robust_rate(texts, _COMMERCIAL_RE)
        cta = _robust_rate(texts, _CTA_RE)
        contact = _robust_rate(texts, _CONTACT_RE)
        link_rate = _robust_rate(texts, _URL_RE)
        price_rate = _robust_rate(texts, _PRICE_RE)
        disclosure = _robust_rate(texts, _DISCLOSURE_RE)
        authentic = _robust_rate(texts, _AUTHENTIC_RE)
        template = self._template_reuse(posts)
        cadence = self._cadence_burst(posts)
        engagement = self._engagement_pattern(posts)
        profile_commerciality = _rms([
            _hit(bio, _COMMERCIAL_RE),
            _hit(bio, _CONTACT_RE),
            _hit(bio, _URL_RE),
            _hit(bio, _OPERATION_RE),
        ])
        cross_pressure = _rms([link_rate, price_rate, _repeated_phrase_pressure(texts)])
        lexical_variation = self._lexical_variation(texts)
        authentic_variation = _clamp(authentic * (0.55 + 0.45 * lexical_variation))
        media_commerciality = _rms([
            _hit(media_text, _COMMERCIAL_RE),
            _hit(media_text, _PRICE_RE),
            _hit(media_text, _CTA_RE),
        ])
        content_operation = _rms([commercial, contact, cta])
        identity_consistency = _clamp(
            math.sqrt(max(0.0, profile_commerciality * content_operation))
            + 0.35 * math.sqrt(max(0.0, media_commerciality * content_operation))
        )

        features = FeatureVector(
            commercial_language=_rms([commercial, price_rate]),
            call_to_action=cta,
            contact_pressure=contact,
            template_reuse=template,
            cadence_burst=cadence,
            engagement_pattern=engagement,
            profile_commerciality=profile_commerciality,
            cross_post_pressure=cross_pressure,
            disclosure_signal=disclosure,
            authentic_variation=authentic_variation,
            media_commerciality=media_commerciality,
            identity_consistency=identity_consistency,
        )

        semantic = SemanticSignal()
        if use_semantic and self.semantic_gateway and self.semantic_gateway.enabled:
            semantic = self.semantic_gateway.inspect(account, media_text)
            if semantic.usable:
                features = self._fuse_semantic(features, semantic)

        evidence = self._evidence(features, posts, semantic)
        missing: list[str] = []
        if len(posts) < 3:
            missing.append("至少 3 条近期内容")
        if len(posts) < 8:
            missing.append("更多近期内容可提高稳定性")
        if not any(p.published_at for p in posts):
            missing.append("发布时间可用于判断发布节奏")
        if not any((p.likes or p.comments or p.shares or p.views) for p in posts):
            missing.append("互动数据可用于交叉验证")
        if not bio:
            missing.append("主页简介")
        if not evidence and texts:
            evidence.append(
                Evidence(
                    "content_context",
                    "暂未发现集中营销线索",
                    "当前样本中的商业表达、导流和模板重复都不突出。",
                    0.45,
                    "against",
                )
            )
        return features, evidence, missing, semantic

    def _fuse_semantic(self, features: FeatureVector, semantic: SemanticSignal) -> FeatureVector:
        values = features.asdict()
        trust = self.calibration.semantic_weight * semantic.grounded_fraction
        for key, score in semantic.values.items():
            if key not in values:
                continue
            base = values[key]
            # Convex fusion keeps the teacher bounded and preserves the owned detector.
            values[key] = _clamp((base + trust * score) / (1.0 + trust))
        return FeatureVector(**values)

    def stability_probe(self, account: AccountSnapshot, media_text: str = "") -> tuple[float, float]:
        cache_key = self._cache_key(account, media_text) + ":stability"
        cached = self._stability_cache.get(cache_key)
        if cached is not None:
            return cached
        posts = [p for p in account.posts if p.text.strip()]
        if len(posts) < 4:
            result = ((0.52 if posts else 0.25), 0.0)
            self._stability_cache[cache_key] = result
            return result
        base_features, _, _, _ = self._extract(account, media_text, use_semantic=False)
        base = self.score(base_features)
        scores: list[float] = []
        for index in range(min(len(posts), 12)):
            reduced = AccountSnapshot(
                platform=account.platform,
                handle=account.handle,
                display_name=account.display_name,
                bio=account.bio,
                followers=account.followers,
                following=account.following,
                verified=account.verified,
                profile_url=account.profile_url,
                posts=[p for i, p in enumerate(posts) if i != index],
            )
            features, _, _, _ = self._extract(reduced, media_text, use_semantic=False)
            scores.append(self.score(features))
        max_swing = max((abs(x - base) for x in scores), default=0.0)
        spread = statistics.pstdev(scores) if len(scores) > 1 else 0.0
        influence = max_swing + 0.75 * spread
        stability = _clamp(math.exp(-3.6 * influence))
        result = (stability, max_swing)
        self._stability_cache[cache_key] = result
        return result

    def _cache_key(self, account: AccountSnapshot, media_text: str) -> str:
        raw = json.dumps(
            {"account": account.asdict(), "media": media_text},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

