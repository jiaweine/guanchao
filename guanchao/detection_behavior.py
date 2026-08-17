from __future__ import annotations

import math
import statistics
from datetime import datetime
from itertools import combinations

from .domain import Evidence, FeatureVector, PostSnapshot
from .detection_support import (
    _AUTHENTIC_RE, _COMMERCIAL_RE, _CONTACT_RE, _CTA_RE, _DISCLOSURE_RE, _PRICE_RE,
    _clamp, _clip_range, _shingles, _tokens,
)
from .semantic import SemanticSignal


class DetectionBehaviorMixin:
    def _evidence(
        self,
        features: FeatureVector,
        posts: list[PostSnapshot],
        semantic: SemanticSignal,
    ) -> list[Evidence]:
        titles = {
            "commercial_language": ("商业表达持续出现", "多条内容反复出现购买、优惠、品牌或强推荐表达。"),
            "call_to_action": ("行动引导明显", "内容频繁引导下单、关注、评论、进入店铺或领取优惠。"),
            "contact_pressure": ("导流线索集中", "资料中多次出现私信、联系方式、进群或商务导流。"),
            "template_reuse": ("内容结构重复", "多条内容在结构和措辞上高度接近，存在批量化生产特征。"),
            "cadence_burst": ("发布节奏偏批量", "发布时间呈现集中发布或近似固定批次节奏。"),
            "engagement_pattern": ("互动形态过于整齐", "不同内容的互动比例异常接近，或深度互动长期偏低。"),
            "profile_commerciality": ("主页长期经营线索清晰", "简介中存在经营、合作、客服、门店或导流信息。"),
            "cross_post_pressure": ("跨内容转化压力持续", "多条内容持续叠加链接、价格或固定转化句式。"),
            "disclosure_signal": ("存在合作披露", "内容中出现广告、赞助或品牌合作等披露。"),
            "authentic_variation": ("个人化表达较丰富", "内容包含具体经历、利弊、犹豫和非模板化叙述。"),
            "media_commerciality": ("素材里出现商业线索", "图片、视频或音频解析结果中出现价格、品牌或转化信息。"),
            "identity_consistency": ("主页与内容长期一致", "主页定位和近期内容中的商业目的持续一致。"),
        }
        items = sorted(
            ((k, getattr(features, k), w) for k, w in self.calibration.weights.items()),
            key=lambda x: abs(x[1] * x[2]),
            reverse=True,
        )
        evidence: list[Evidence] = []
        for key, value, weight in items[:8]:
            if value < 0.16:
                continue
            title, detail = titles[key]
            direction = "against" if weight < 0 else ("context" if key == "disclosure_signal" else "supports")
            ids = semantic.post_ids.get(key) or self._example_posts(posts, key)
            evidence.append(
                Evidence(
                    key,
                    title,
                    detail,
                    _clamp(abs(value * weight) / 1.25),
                    direction,
                    ids,
                    [],
                )
            )
        return evidence

    def _confidence(
        self,
        account: AccountSnapshot,
        features: FeatureVector,
        stability: float,
        marketing: float,
        semantic_grounded: float,
    ) -> float:
        posts = [p for p in account.posts if p.text.strip()]
        n = len(posts)
        sample = 1.0 - math.exp(-n / 4.0) if n else 0.0
        channels = [
            bool(account.bio),
            any(p.published_at for p in posts),
            any(p.views or p.likes or p.comments or p.shares for p in posts),
            bool(account.followers),
            features.media_commerciality > 0,
            semantic_grounded > 0,
        ]
        metadata = sum(channels) / len(channels)
        margin = min(1.0, abs(marketing - 0.5) * 2.0)
        evidence_certainty = 1.0 - math.exp(-(0.8 + n / 4.0) * margin * margin)
        base = 0.10 + 0.46 * sample + 0.17 * metadata + 0.27 * evidence_certainty
        return _clamp(base * (0.70 + 0.30 * stability))

    @staticmethod
    def _swing_limit(sample_size: int) -> float:
        # With more observations, a single post should have less leverage.
        return _clip_range(0.34 / math.sqrt(max(1.0, sample_size / 2.0)), 0.12, 0.28)

    def _label(self, score: float, confidence: float, stability: float) -> str:
        uncertainty = max(1.0 - confidence, 1.0 - stability)
        abstain = _clip_range(
            self.calibration.abstain_margin * (1.0 + uncertainty),
            self.calibration.abstain_margin,
            0.22,
        )
        threshold = self.calibration.decision_threshold
        if confidence < 0.42:
            return "证据不足"
        if threshold - abstain <= score <= threshold + abstain:
            return "存在部分营销信号"
        if score >= self.calibration.high_threshold and confidence >= 0.66:
            return "高度营销化"
        if score > threshold + abstain:
            return "明显营销倾向"
        return "更像普通创作者"

    @staticmethod
    def _summary(label: str, confidence: float, evidence: list[Evidence], missing: list[str]) -> str:
        if label == "证据不足":
            return "当前资料还不足以稳定判断。建议补充近期内容或可核对的素材后再复核。"
        support = [e.title for e in evidence if e.direction == "supports"][:2]
        against = [e.title for e in evidence if e.direction == "against"][:1]
        parts = [f"当前判断为“{label}”，把握度约 {round(confidence * 100)}%。"]
        if support:
            parts.append("主要依据是" + "、".join(support) + "。")
        if against:
            parts.append("同时存在“" + against[0] + "”这类反向线索。")
        if missing:
            parts.append("补充资料后可以继续复核。")
        return "".join(parts)

    def _template_reuse(self, posts: list[PostSnapshot]) -> float:
        if len(posts) < 2:
            return 0.0
        vectors = [_shingles(p.text) for p in posts[:20]]
        sims = [len(a & b) / max(1, len(a | b)) for a, b in combinations(vectors, 2) if a and b]
        if not sims:
            return 0.0
        top = sorted(sims, reverse=True)[: max(1, min(8, len(sims)))]
        return _clamp(statistics.fmean(top) * 1.8)

    def _lexical_variation(self, texts: list[str]) -> float:
        if len(texts) < 2:
            return 0.2 if texts else 0.0
        token_sets = [set(_tokens(t)) for t in texts]
        unique = [len(s) / max(1, len(_tokens(t))) for s, t in zip(token_sets, texts)]
        lengths = [len(t) for t in texts]
        cv = statistics.pstdev(lengths) / max(1.0, statistics.fmean(lengths))
        return _clamp(0.55 * statistics.fmean(unique) + 0.45 * min(1.0, cv * 1.8))

    def _cadence_burst(self, posts: list[PostSnapshot]) -> float:
        times: list[datetime] = []
        for post in posts:
            if not post.published_at:
                continue
            try:
                times.append(datetime.fromisoformat(post.published_at.replace("Z", "+00:00")))
            except ValueError:
                continue
        if len(times) < 3:
            return 0.0
        times.sort()
        intervals = [(b - a).total_seconds() / 3600 for a, b in zip(times, times[1:])]
        mean = statistics.fmean(intervals)
        if mean <= 0:
            return 1.0
        cv = statistics.pstdev(intervals) / mean if len(intervals) > 1 else 0.0
        short_share = sum(1 for x in intervals if x <= 2.0) / len(intervals)
        return _clamp(0.58 * short_share + 0.42 * (1.0 - min(1.0, cv)))

    def _engagement_pattern(self, posts: list[PostSnapshot]) -> float:
        with_views = [p for p in posts if p.views > 0]
        if len(with_views) >= 3:
            ratios = [(p.likes + 2 * p.comments + 2 * p.shares) / p.views for p in with_views]
            mean = statistics.fmean(ratios)
            cv = statistics.pstdev(ratios) / max(0.0001, mean)
            shallow = statistics.fmean([p.comments / max(1, p.likes) for p in with_views])
            return _clamp(
                0.58 * _clamp((0.18 - min(0.18, cv)) / 0.18)
                + 0.42 * _clamp((0.035 - min(0.035, shallow)) / 0.035)
            )
        return 0.0

    @staticmethod
    def _example_posts(posts: list[PostSnapshot], key: str) -> list[str]:
        patterns = {
            "commercial_language": _COMMERCIAL_RE,
            "call_to_action": _CTA_RE,
            "contact_pressure": _CONTACT_RE,
            "cross_post_pressure": _PRICE_RE,
            "disclosure_signal": _DISCLOSURE_RE,
            "authentic_variation": _AUTHENTIC_RE,
        }
        if key == "template_reuse":
            return [p.id for p in posts[:3]]
        pattern = patterns.get(key)
        return (
            [p.id for p in posts if pattern and pattern.search(p.text)][:3]
            if pattern
            else [p.id for p in posts[:2]]
        )
