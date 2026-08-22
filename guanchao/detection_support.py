from __future__ import annotations

import math
import re
from typing import Iterable

from .domain import FeatureVector

_WORD_RE = re.compile(r"[\u4e00-\u9fff]{1,4}|[A-Za-z0-9_]+")
_URL_RE = re.compile(r"https?://|www\.|(?:tb|t)\.cn|小店|橱窗|购物车|小黄车", re.I)
_CONTACT_RE = re.compile(r"(?:vx|v信|微信|薇信|私信|私我|私聊|小窗|加我|加v|联系|客服|进群|群聊|商务合作|合作请|邮箱|email)", re.I)
_PRICE_RE = re.compile(r"(?:¥|￥|元|到手|券后|立减|满\d+减\d+|\d+(?:\.\d+)?折|优惠|福利|秒杀|限时|返现)", re.I)
_DISCLOSURE_RE = re.compile(r"(?:广告|赞助|品牌合作|商业合作|合作内容|推广|体验官|受邀|试用)")
_CTA_RE = re.compile(r"(?:点击|戳|下单|购买|入手|冲(?!洗)|闭眼入|收藏|关注|转发|评论区|链接|主页|橱窗|小黄车|领券|领取|私信|咨询|进店|复制口令|置顶|固定(?:那条|内容)|入口|扣(?:个)?[01]|统一发|发你|想要同款|问链接|关键词|预约|名额)")
_COMMERCIAL_RE = re.compile(r"(?:新品|上新|补货|返场|爆款|同款|官方|旗舰|品牌|福利|专属|种草|必买|安利|测评|推荐|性价比|低至|到手|库存|现货|包邮|团购|代购|店铺|直播间|定制|打样|样衣)")
_AUTHENTIC_RE = re.compile(r"(?:我原本|后来|没想到|踩坑|不适合|缺点|但是|不过|真实感受|用了\d+天|用了\d+周|个人体验|仅代表个人|我自己|我还是|其实没有)")
_OPERATION_RE = re.compile(r"(?:商务|合作|客服|门店|直播|橱窗|团购|招商|选品|供应链|矩阵|工作室|顾问|运营|投放|定制)")

# Feature extraction is intentionally bounded. Imported text is untrusted and
# several helpers create token/shingle sets; clipping here prevents a single
# pathological post from turning an otherwise linear investigation into a large
# memory allocation.
MAX_PATTERN_TEXT_CHARS = 4_000
MAX_PATTERN_POSTS = 80

_INTERACTIONS: dict[str, tuple[str, str]] = {
    "intent_action": ("commercial_language", "call_to_action"),
    "action_contact": ("call_to_action", "contact_pressure"),
    "profile_conversion": ("profile_commerciality", "cross_post_pressure"),
    "template_cadence": ("template_reuse", "cadence_burst"),
    "media_identity": ("media_commerciality", "identity_consistency"),
    "commercial_authentic": ("commercial_language", "authentic_variation"),
    "action_persistence": ("call_to_action", "call_to_action"),
    "contact_persistence": ("contact_pressure", "contact_pressure"),
}


def interaction_value(features: FeatureVector, key: str) -> float:
    pair = _INTERACTIONS.get(key)
    if not pair:
        return 0.0
    return (
        max(0.0, float(getattr(features, pair[0], 0.0)))
        * max(0.0, float(getattr(features, pair[1], 0.0)))
    )


def _tokens(text: str) -> list[str]:
    bounded = (text or "")[:MAX_PATTERN_TEXT_CHARS]
    return [m.group(0).lower() for m in _WORD_RE.finditer(bounded)]


def _shingles(text: str, size: int = 3) -> set[str]:
    compact = re.sub(r"\s+", "", (text or "")[:MAX_PATTERN_TEXT_CHARS].lower())
    compact = re.sub(r"\d+", "#", compact)
    return (
        {compact}
        if compact and len(compact) < size
        else ({compact[i : i + size] for i in range(len(compact) - size + 1)} if compact else set())
    )


def _hit(text: str, pattern: re.Pattern[str]) -> float:
    return 1.0 if text and pattern.search(text[:MAX_PATTERN_TEXT_CHARS]) else 0.0


def _robust_rate(texts: Iterable[str], pattern: re.Pattern[str]) -> float:
    """Frequency evidence with Wilson shrinkage for tiny samples.

    A single matching post should not look as reliable as the same pattern repeated
    across a long account history. Density preserves repeated cues inside a post,
    while the Wilson lower bound supplies the sample-size penalty.
    """
    items = [(t or "")[:MAX_PATTERN_TEXT_CHARS] for t in texts if t][:MAX_PATTERN_POSTS]
    if not items:
        return 0.0
    hits = sum(1 for t in items if pattern.search(t))
    lower = _wilson_lower(hits, len(items))
    density = sum(min(3, len(pattern.findall(t))) for t in items) / (3 * len(items))
    support = 1.0 - math.exp(-len(items) / 4.0)
    return _clamp((0.74 * lower + 0.26 * density) * (0.82 + 0.18 * support))


def _wilson_lower(hits: int, total: int, z: float = 1.0) -> float:
    if total <= 0 or hits <= 0:
        return 0.0
    p = hits / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return _clamp((centre - spread) / denominator)


def _repeated_phrase_pressure(texts: list[str]) -> float:
    bounded = [
        (text or "")[:MAX_PATTERN_TEXT_CHARS]
        for text in texts[:MAX_PATTERN_POSTS]
        if text
    ]
    if len(bounded) < 2:
        return 0.0
    chunks: dict[str, int] = {}
    for text in bounded:
        normalized = re.sub(r"\s+", "", text)
        seen = set()
        for n in (5, 6, 7):
            for i in range(max(0, len(normalized) - n + 1)):
                chunk = normalized[i : i + n]
                if chunk in seen or not chunk.strip():
                    continue
                seen.add(chunk)
                chunks[chunk] = chunks.get(chunk, 0) + 1
    repeated = sum(1 for count in chunks.values() if count >= max(2, math.ceil(len(bounded) * 0.4)))
    return _clamp(repeated / 24)


def _rms(values: Iterable[float]) -> float:
    rows = [max(0.0, float(value)) for value in values]
    if not rows:
        return 0.0
    return _clamp(math.sqrt(sum(value * value for value in rows) / len(rows)))


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _clip_range(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
