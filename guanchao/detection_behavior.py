from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from itertools import combinations

from .domain import AccountSnapshot, Evidence, FeatureVector, PostSnapshot
from .detection_support import (
    MAX_PATTERN_POSTS,
    _AUTHENTIC_RE,
    _COMMERCIAL_RE,
    _CONTACT_RE,
    _CTA_RE,
    _DISCLOSURE_RE,
    _PRICE_RE,
    _clamp,
    _shingles,
    _tokens,
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
            "cadence_burst": ("发布节奏呈现规律", "发布时间呈现集中或近似固定的重复节奏。"),
            "engagement_pattern": ("互动形态较整齐", "不同内容的互动比例相近，深度互动相对有限。"),
            "profile_commerciality": ("主页长期经营线索清晰", "简介中存在经营、合作、客服、门店或导流信息。"),
            "cross_post_pressure": ("跨内容转化压力持续", "多条内容持续叠加链接、价格或固定转化句式。"),
            "disclosure_signal": ("存在合作披露", "内容中出现广告、赞助或品牌合作等披露。"),
            "authentic_variation": ("个人化表达较丰富", "内容包含具体经历、利弊、犹豫和非模板化叙述。"),
            "media_commerciality": ("素材里出现商业线索", "图片、视频或音频解析结果中出现价格、品牌或转化信息。"),
            "identity_consistency": ("主页与内容长期一致", "主页定位和近期内容中的商业目的持续一致。"),
        }
        ranked = sorted(
            (
                (key, float(getattr(features, key)), weight)
                for key, weight in self.calibration.weights.items()
                if key in titles
            ),
            key=lambda row: abs(row[1] * row[2]),
            reverse=True,
        )
        evidence: list[Evidence] = []
        for key, value, weight in ranked:
            contribution = abs(value * weight)
            if contribution <= 0.0:
                continue
            title, detail = titles[key]
            direction = (
                "against"
                if weight < 0
                else "context"
                if key == "disclosure_signal"
                else "supports"
            )
            ids = semantic.post_ids.get(key) or self._example_posts(posts, key)
            evidence.append(
                Evidence(
                    key,
                    title,
                    detail,
                    contribution / (1.0 + contribution),
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
        posts = [post for post in account.posts if post.text.strip()]
        n = len(posts)
        sample_support = n / (n + 1.0) if n else 0.0
        channels = [
            bool(account.bio),
            any(post.published_at for post in posts),
            any(post.views or post.likes or post.comments or post.shares for post in posts),
            bool(account.followers),
            features.media_commerciality > 0,
            semantic_grounded > 0,
        ]
        metadata_support = sum(channels) / len(channels)
        threshold = self.calibration.decision_threshold
        span = max(threshold, 1.0 - threshold, 1e-6)
        separation = min(1.0, abs(marketing - threshold) / span)
        components = [sample_support, metadata_support, separation, stability]
        if any(component <= 0.0 for component in components):
            return 0.0
        return _clamp(math.prod(components) ** (1.0 / len(components)))

    def _label(self, score: float, confidence: float, stability: float) -> str:
        threshold = self.calibration.decision_threshold
        maximum_band = max(0.0, min(threshold, 1.0 - threshold))
        base_band = min(maximum_band, max(0.0, self.calibration.abstain_margin))
        uncertainty = max(1.0 - confidence, 1.0 - stability)
        abstain = base_band + uncertainty * (maximum_band - base_band)
        if threshold - abstain <= score <= threshold + abstain:
            return "存在部分营销信号"
        if score >= self.calibration.high_threshold:
            return "高度营销化"
        if score > threshold + abstain:
            return "明显营销倾向"
        return "更像普通创作者"

    @staticmethod
    def _summary(
        label: str, confidence: float, evidence: list[Evidence], missing: list[str]
    ) -> str:
        support = [item.title for item in evidence if item.direction == "supports"][:2]
        against = [item.title for item in evidence if item.direction == "against"][:1]
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
        vectors = [_shingles(post.text) for post in posts[:20]]
        similarities = [
            len(first & second) / max(1, len(first | second))
            for first, second in combinations(vectors, 2)
            if first and second
        ]
        return _clamp(statistics.fmean(similarities)) if similarities else 0.0

    def _lexical_variation(self, texts: list[str]) -> float:
        bounded = texts[: min(40, MAX_PATTERN_POSTS)]
        if not bounded:
            return 0.0
        if len(bounded) == 1:
            tokens = _tokens(bounded[0])
            return len(set(tokens)) / max(1, len(tokens))
        token_sets = [set(_tokens(text)) for text in bounded]
        similarities = [
            len(first & second) / max(1, len(first | second))
            for first, second in combinations(token_sets, 2)
            if first or second
        ]
        return _clamp(1.0 - statistics.fmean(similarities)) if similarities else 0.0

    def _cadence_burst(self, posts: list[PostSnapshot]) -> float:
        times: list[datetime] = []
        for post in posts[:MAX_PATTERN_POSTS]:
            if not post.published_at:
                continue
            try:
                parsed = datetime.fromisoformat(post.published_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            # Imported datasets often mix `2026-08-01T12:00:00` and explicit
            # offset timestamps. Python refuses to sort naive and aware datetimes;
            # treating naive import timestamps as UTC keeps cadence deterministic
            # instead of crashing a whole investigation.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            times.append(parsed)
        if len(times) < 3:
            return 0.0
        times.sort()
        intervals = [
            (right - left).total_seconds()
            for left, right in zip(times, times[1:])
            if (right - left).total_seconds() >= 0
        ]
        if not intervals:
            return 0.0
        mean = statistics.fmean(intervals)
        if mean <= 0:
            return 1.0
        cv = statistics.pstdev(intervals) / mean if len(intervals) > 1 else 0.0
        return 1.0 / (1.0 + cv)

    def _engagement_pattern(self, posts: list[PostSnapshot]) -> float:
        with_views = [post for post in posts[:MAX_PATTERN_POSTS] if post.views > 0]
        if len(with_views) < 2:
            return 0.0
        ratios = [
            (post.likes + post.comments + post.shares) / post.views
            for post in with_views
        ]
        mean = statistics.fmean(ratios)
        cv = statistics.pstdev(ratios) / max(mean, 1e-9)
        regularity = 1.0 / (1.0 + cv)
        depth = statistics.fmean(
            (post.comments + post.shares) / max(1, post.likes + post.comments + post.shares)
            for post in with_views
        )
        shallowness = 1.0 - _clamp(depth)
        return _clamp(math.sqrt(regularity * shallowness))

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
            return [post.id for post in posts[:3]]
        pattern = patterns.get(key)
        return (
            [post.id for post in posts if pattern and pattern.search(post.text)][:3]
            if pattern
            else [post.id for post in posts[:2]]
        )
