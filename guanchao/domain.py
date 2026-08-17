from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Platform = Literal["xiaohongshu", "weibo", "douyin", "bilibili", "other"]
AssetKind = Literal["image", "video", "audio", "document", "other"]
AssetStatus = Literal["pending", "ready", "error"]
_ALLOWED_PLATFORMS = {"xiaohongshu", "weibo", "douyin", "bilibili", "other"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class PostSnapshot:
    id: str
    text: str
    published_at: str | None = None
    likes: int = 0
    comments: int = 0
    shares: int = 0
    views: int = 0
    url: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int = 0) -> "PostSnapshot":
        if not isinstance(raw, dict):
            raise TypeError("post must be an object")
        stamp = raw.get("published_at") or raw.get("timestamp")
        return cls(
            id=str(raw.get("id") or f"post-{index + 1}"),
            text=str(raw.get("text") or raw.get("caption") or raw.get("title") or "").strip(),
            published_at=str(stamp) if stamp is not None else None,
            likes=_safe_int(raw.get("likes")),
            comments=_safe_int(raw.get("comments")),
            shares=_safe_int(raw.get("shares")),
            views=_safe_int(raw.get("views")),
            url=str(raw.get("url")) if raw.get("url") else None,
        )


@dataclass(slots=True)
class AccountSnapshot:
    platform: Platform
    handle: str
    display_name: str = ""
    bio: str = ""
    followers: int = 0
    following: int = 0
    verified: bool = False
    profile_url: str | None = None
    posts: list[PostSnapshot] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AccountSnapshot":
        if not isinstance(raw, dict):
            raise TypeError("account must be an object")
        posts_raw = raw.get("posts") or []
        if not isinstance(posts_raw, list):
            raise TypeError("posts must be a list")
        platform = str(raw.get("platform") or "other").strip().lower()
        if platform not in _ALLOWED_PLATFORMS:
            platform = "other"
        return cls(
            platform=platform,  # type: ignore[arg-type]
            handle=str(raw.get("handle") or raw.get("account") or "unknown").strip() or "unknown",
            display_name=str(raw.get("display_name") or raw.get("name") or "").strip(),
            bio=str(raw.get("bio") or "").strip(),
            followers=_safe_int(raw.get("followers")),
            following=_safe_int(raw.get("following")),
            verified=bool(raw.get("verified") or False),
            profile_url=str(raw.get("profile_url") or raw.get("url")) if (raw.get("profile_url") or raw.get("url")) else None,
            posts=[PostSnapshot.from_dict(item, i) for i, item in enumerate(posts_raw)],
        )

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AssetSnapshot:
    id: str
    case_id: str
    name: str
    kind: AssetKind
    content_type: str
    size: int
    status: AssetStatus = "pending"
    extracted_text: str = ""
    note: str = ""
    error: str = ""
    created_at: str = field(default_factory=utcnow_iso)

    def asdict(self, include_text: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        if not include_text:
            payload.pop("extracted_text", None)
        return payload


@dataclass(slots=True)
class Evidence:
    key: str
    title: str
    detail: str
    strength: float
    direction: Literal["supports", "against", "context"] = "supports"
    post_ids: list[str] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FeatureVector:
    commercial_language: float = 0.0
    call_to_action: float = 0.0
    contact_pressure: float = 0.0
    template_reuse: float = 0.0
    cadence_burst: float = 0.0
    engagement_pattern: float = 0.0
    profile_commerciality: float = 0.0
    cross_post_pressure: float = 0.0
    disclosure_signal: float = 0.0
    authentic_variation: float = 0.0
    media_commerciality: float = 0.0
    identity_consistency: float = 0.0

    def asdict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class DetectionResult:
    marketing_likelihood: float
    covert_promotion_risk: float
    confidence: float
    stability: float
    label: str
    summary: str
    features: FeatureVector
    evidence: list[Evidence]
    missing: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolResult:
    tool: str
    ok: bool
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    error: str | None = None

    def asdict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "summary": self.summary,
            "payload": self.payload,
            "evidence": [item.asdict() for item in self.evidence],
            "error": self.error,
        }


@dataclass(slots=True)
class RunEvent:
    at: str
    kind: str
    title: str
    detail: str = ""
    tool: str | None = None
    status: Literal["working", "done", "warning", "error"] = "done"

    @classmethod
    def create(cls, kind: str, title: str, detail: str = "", tool: str | None = None,
               status: Literal["working", "done", "warning", "error"] = "done") -> "RunEvent":
        return cls(at=utcnow_iso(), kind=kind, title=title, detail=detail, tool=tool, status=status)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)
