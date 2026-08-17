from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Iterable

from .domain import AccountSnapshot, DetectionResult, Evidence, FeatureVector, PostSnapshot

_WORD_RE = re.compile(r"[\u4e00-\u9fff]{1,4}|[A-Za-z0-9_]+")
_URL_RE = re.compile(r"https?://|www\.|(?:tb|t)\.cn|小店|橱窗|购物车", re.I)
_CONTACT_RE = re.compile(r"(?:vx|v信|微信|薇信|私信|加我|联系|客服|进群|群聊|商务合作|合作请|邮箱|email)", re.I)
_PRICE_RE = re.compile(r"(?:¥|￥|元|到手|券后|立减|满\d+减\d+|折|优惠|福利|秒杀|限时|返现)", re.I)
_DISCLOSURE_RE = re.compile(r"(?:广告|赞助|品牌合作|商业合作|合作内容|推广|体验官|受邀|试用)")
_CTA_RE = re.compile(r"(?:点击|戳|下单|购买|入手|冲(?!洗)|闭眼入|收藏|关注|转发|评论区|链接|主页|橱窗|小黄车|领券|领取|私信|咨询|进店|搜索|复制)")
_COMMERCIAL_RE = re.compile(r"(?:新品|爆款|同款|官方|旗舰|品牌|福利|种草|必买|安利|测评|推荐|性价比|低至|到手|库存|现货|包邮|团购|代购|店铺|直播间)")
_AUTHENTIC_RE = re.compile(r"(?:我原本|后来|没想到|踩坑|不适合|缺点|但是|不过|真实感受|用了\d+天|用了\d+周|个人体验|仅代表个人)")


@dataclass(slots=True)
class Calibration:
    bias: float = -2.15
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "commercial_language": 1.42,
            "call_to_action": 1.18,
            "contact_pressure": 1.08,
            "template_reuse": 1.22,
            "cadence_burst": 0.72,
            "engagement_pattern": 0.54,
            "profile_commerciality": 0.86,
            "cross_post_pressure": 0.78,
            "disclosure_signal": 0.22,
            "authentic_variation": -0.76,
        }
    )

    def to_dict(self) -> dict[str, object]:
        return {"bias": self.bias, "weights": dict(self.weights)}

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
        return cls(bias=bias, weights=weights)


class MarketingDetector:
    """Owned, deterministic detector used by the harness and replay evaluator."""

    def __init__(self, calibration: Calibration | None = None):
        self.calibration = calibration or Calibration()

    def analyze(self, account: AccountSnapshot) -> DetectionResult:
        features, evidence, missing = self.extract(account)
        marketing = self.score(features)
        disclosure_gap = max(0.0, 1.0 - features.disclosure_signal)
        covert = _clamp(marketing * (0.55 + 0.45 * disclosure_gap) * (0.88 + 0.12 * features.contact_pressure))
        confidence = self._confidence(account, features)
        label = self._label(marketing, confidence)
        summary = self._summary(label, marketing, confidence, evidence, missing)
        return DetectionResult(
            marketing_likelihood=marketing,
            covert_promotion_risk=covert,
            confidence=confidence,
            label=label,
            summary=summary,
            features=features,
            evidence=evidence,
            missing=missing,
        )

    def score(self, features: FeatureVector) -> float:
        linear = self.calibration.bias
        for key, weight in self.calibration.weights.items():
            linear += weight * float(getattr(features, key))
        return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, linear))))

    def extract(self, account: AccountSnapshot) -> tuple[FeatureVector, list[Evidence], list[str]]:
        posts = [p for p in account.posts if p.text.strip()]
        texts = [p.text for p in posts]
        joined = "\n".join(texts)
        bio = account.bio

        commercial = _rate(texts, _COMMERCIAL_RE)
        cta = _rate(texts, _CTA_RE)
        contact = _rate(texts, _CONTACT_RE)
        link_rate = _rate(texts, _URL_RE)
        price_rate = _rate(texts, _PRICE_RE)
        disclosure = _rate(texts, _DISCLOSURE_RE)
        authentic = _rate(texts, _AUTHENTIC_RE)
        template = self._template_reuse(posts)
        cadence = self._cadence_burst(posts)
        engagement = self._engagement_pattern(posts)
        profile_commerciality = _clamp(
            0.40 * _hit(bio, _COMMERCIAL_RE)
            + 0.30 * _hit(bio, _CONTACT_RE)
            + 0.20 * _hit(bio, _URL_RE)
            + 0.10 * (1.0 if account.verified and (_hit(bio, _COMMERCIAL_RE) or _hit(bio, _CONTACT_RE)) else 0.0)
        )
        cross_pressure = _clamp(0.48 * link_rate + 0.30 * price_rate + 0.22 * _repeated_phrase_pressure(texts))
        authentic_variation = _clamp(0.55 * authentic + 0.45 * self._lexical_variation(texts))

        features = FeatureVector(
            commercial_language=_clamp(0.74 * commercial + 0.26 * price_rate),
            call_to_action=cta,
            contact_pressure=contact,
            template_reuse=template,
            cadence_burst=cadence,
            engagement_pattern=engagement,
            profile_commerciality=profile_commerciality,
            cross_post_pressure=cross_pressure,
            disclosure_signal=disclosure,
            authentic_variation=authentic_variation,
        )

        evidence: list[Evidence] = []
        strongest = sorted(
            ((key, getattr(features, key), weight) for key, weight in self.calibration.weights.items()),
            key=lambda item: abs(item[1] * item[2]),
            reverse=True,
        )
        titles = {
            "commercial_language": ("商业表达集中", "多条内容反复出现购买、优惠、品牌或强推荐表达。"),
            "call_to_action": ("行动引导明显", "内容频繁引导点击、下单、关注、评论或进入店铺。"),
            "contact_pressure": ("导流线索集中", "资料中多次出现私信、联系方式、进群或商务导流。"),
            "template_reuse": ("内容模板重复", "多条内容在结构和措辞上高度接近，像批量化生产。"),
            "cadence_burst": ("发布节奏偏批量", "发布时间呈现明显的集中发布或固定批次节奏。"),
            "engagement_pattern": ("互动结构异常", "不同内容的互动比例过于整齐，或浏览与深度互动不协调。"),
            "profile_commerciality": ("主页商业目的清晰", "简介或主页信息中存在经营、合作或导流线索。"),
            "cross_post_pressure": ("跨内容转化压力持续", "多条内容持续叠加链接、价格、店铺或固定转化句式。"),
            "disclosure_signal": ("合作披露较多", "内容中存在广告、赞助或品牌合作等明确披露。"),
            "authentic_variation": ("个人表达变化丰富", "内容中有较多具体经历、利弊表达和不规则叙述。"),
        }
        for key, value, weight in strongest[:6]:
            if value < 0.18:
                continue
            title, detail = titles[key]
            direction = "against" if weight < 0 else ("context" if key == "disclosure_signal" else "supports")
            evidence.append(
                Evidence(
                    key=key,
                    title=title,
                    detail=detail,
                    strength=_clamp(abs(value * weight) / 1.45),
                    direction=direction,
                    post_ids=self._example_posts(posts, key),
                )
            )

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

        if joined and not evidence:
            evidence.append(
                Evidence(
                    key="content_context",
                    title="暂未发现集中营销线索",
                    detail="当前样本中的商业表达、导流和模板重复都不突出。",
                    strength=0.45,
                    direction="against",
                )
            )
        return features, evidence, missing

    def _confidence(self, account: AccountSnapshot, features: FeatureVector) -> float:
        n = len([p for p in account.posts if p.text.strip()])
        sample = min(1.0, math.log1p(n) / math.log(13)) if n else 0.0
        metadata = 0.0
        metadata += 0.25 if account.bio else 0.0
        metadata += 0.25 if any(p.published_at for p in account.posts) else 0.0
        metadata += 0.25 if any(p.views or p.likes or p.comments or p.shares for p in account.posts) else 0.0
        metadata += 0.25 if account.followers else 0.0
        vals = [v for k, v in features.asdict().items() if k != "disclosure_signal"]
        separation = min(1.0, (max(vals, default=0.0) + statistics.fmean(vals or [0.0])) / 1.25)
        return _clamp(0.18 + 0.52 * sample + 0.18 * metadata + 0.12 * separation)

    @staticmethod
    def _label(score: float, confidence: float) -> str:
        if confidence < 0.42:
            return "证据不足"
        if score >= 0.78:
            return "高度营销化"
        if score >= 0.58:
            return "明显营销倾向"
        if score >= 0.40:
            return "存在部分营销信号"
        return "更像普通创作者"

    @staticmethod
    def _summary(label: str, score: float, confidence: float, evidence: list[Evidence], missing: list[str]) -> str:
        if label == "证据不足":
            return "当前资料还不足以稳定判断。建议补充近期内容后再复核。"
        support = [e.title for e in evidence if e.direction == "supports"][:2]
        against = [e.title for e in evidence if e.direction == "against"][:1]
        parts = [f"当前判断为“{label}”，把握度约 {round(confidence * 100)}%。"]
        if support:
            parts.append("主要依据是" + "、".join(support) + "。")
        if against:
            parts.append("同时存在“" + against[0] + "”这类反向线索。")
        if missing:
            parts.append("继续补充资料可以进一步提高稳定性。")
        return "".join(parts)

    def _template_reuse(self, posts: list[PostSnapshot]) -> float:
        if len(posts) < 2:
            return 0.0
        vectors = [_shingles(p.text) for p in posts[:20]]
        sims = []
        for a, b in combinations(vectors, 2):
            if not a or not b:
                continue
            sims.append(len(a & b) / max(1, len(a | b)))
        if not sims:
            return 0.0
        top = sorted(sims, reverse=True)[: max(1, min(8, len(sims)))]
        return _clamp(statistics.fmean(top) * 1.8)

    def _lexical_variation(self, texts: list[str]) -> float:
        if len(texts) < 2:
            return 0.2 if texts else 0.0
        token_sets = [set(_tokens(t)) for t in texts]
        unique_ratios = [len(s) / max(1, len(_tokens(t))) for s, t in zip(token_sets, texts)]
        lengths = [len(t) for t in texts]
        cv = statistics.pstdev(lengths) / max(1.0, statistics.fmean(lengths))
        return _clamp(0.55 * statistics.fmean(unique_ratios) + 0.45 * min(1.0, cv * 1.8))

    def _cadence_burst(self, posts: list[PostSnapshot]) -> float:
        times: list[datetime] = []
        for post in posts:
            if not post.published_at:
                continue
            try:
                value = post.published_at.replace("Z", "+00:00")
                times.append(datetime.fromisoformat(value))
            except ValueError:
                continue
        if len(times) < 3:
            return 0.0
        times.sort()
        intervals = [(b - a).total_seconds() / 3600 for a, b in zip(times, times[1:])]
        if not intervals:
            return 0.0
        mean = statistics.fmean(intervals)
        if mean <= 0:
            return 1.0
        cv = statistics.pstdev(intervals) / mean if len(intervals) > 1 else 0.0
        short_share = sum(1 for x in intervals if x <= 2.0) / len(intervals)
        fixed_share = 1.0 - min(1.0, cv)
        return _clamp(0.58 * short_share + 0.42 * fixed_share)

    def _engagement_pattern(self, posts: list[PostSnapshot]) -> float:
        with_views = [p for p in posts if p.views > 0]
        if len(with_views) >= 3:
            ratios = [(p.likes + 2 * p.comments + 2 * p.shares) / p.views for p in with_views]
            mean = statistics.fmean(ratios)
            cv = statistics.pstdev(ratios) / max(0.0001, mean)
            shallow = statistics.fmean([p.comments / max(1, p.likes) for p in with_views])
            too_uniform = _clamp((0.18 - min(0.18, cv)) / 0.18)
            shallow_signal = _clamp((0.035 - min(0.035, shallow)) / 0.035)
            return _clamp(0.58 * too_uniform + 0.42 * shallow_signal)
        with_likes = [p for p in posts if p.likes > 0]
        if len(with_likes) >= 3:
            ratios = [p.comments / max(1, p.likes) for p in with_likes]
            shallow = statistics.fmean(ratios)
            return _clamp((0.025 - min(0.025, shallow)) / 0.025)
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
        if not pattern:
            return [p.id for p in posts[:2]]
        return [p.id for p in posts if pattern.search(p.text)][:3]


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD_RE.finditer(text)]


def _shingles(text: str, size: int = 3) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    compact = re.sub(r"\d+", "#", compact)
    if len(compact) < size:
        return {compact} if compact else set()
    return {compact[i : i + size] for i in range(len(compact) - size + 1)}


def _hit(text: str, pattern: re.Pattern[str]) -> float:
    return 1.0 if text and pattern.search(text) else 0.0


def _rate(texts: Iterable[str], pattern: re.Pattern[str]) -> float:
    items = [t for t in texts if t]
    if not items:
        return 0.0
    hits = sum(1 for t in items if pattern.search(t))
    density = sum(min(3, len(pattern.findall(t))) for t in items) / (3 * len(items))
    return _clamp(0.72 * hits / len(items) + 0.28 * density)


def _repeated_phrase_pressure(texts: list[str]) -> float:
    if len(texts) < 2:
        return 0.0
    chunks: dict[str, int] = {}
    for text in texts:
        normalized = re.sub(r"\s+", "", text)
        seen = set()
        for n in (5, 6, 7):
            for i in range(max(0, len(normalized) - n + 1)):
                chunk = normalized[i : i + n]
                if chunk in seen or not chunk.strip():
                    continue
                seen.add(chunk)
                chunks[chunk] = chunks.get(chunk, 0) + 1
    repeated = sum(1 for count in chunks.values() if count >= max(2, math.ceil(len(texts) * 0.4)))
    return _clamp(repeated / 24)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
