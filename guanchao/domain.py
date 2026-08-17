from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Platform = Literal["xiaohongshu", "weibo", "douyin", "bilibili", "other"]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        return cls(
            id=str(raw.get("id") or f"post-{index + 1}"),
            text=str(raw.get("text") or raw.get("caption") or raw.get("title") or "").strip(),
            published_at=(str(raw.get("published_at") or raw.get("timestamp")) if (raw.get("published_at") or raw.get("timestamp")) is not None else None),
            likes=max(0, int(raw.get("likes") or 0)),
            comments=max(0, int(raw.get("comments") or 0)),
            shares=max(0, int(raw.get("shares") or 0)),
            views=max(0, int(raw.get("views") or 0)),
            url=raw.get("url"),
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
        posts_raw = raw.get("posts") or []
        return cls(
            platform=raw.get("platform", "other"),
            handle=str(raw.get("handle") or raw.get("account") or "unknown").strip(),
            display_name=str(raw.get("display_name") or raw.get("name") or "").strip(),
            bio=str(raw.get("bio") or "").strip(),
            followers=max(0, int(raw.get("followers") or 0)),
            following=max(0, int(raw.get("following") or 0)),
            verified=bool(raw.get("verified") or False),
            profile_url=raw.get("profile_url") or raw.get("url"),
            posts=[PostSnapshot.from_dict(item, i) for i, item in enumerate(posts_raw)],
        )

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Evidence:
    key: str
    title: str
    detail: str
    strength: float
    direction: Literal["supports", "against", "context"] = "supports"
    post_ids: list[str] = field(default_factory=list)

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

    def asdict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class DetectionResult:
    marketing_likelihood: float
    covert_promotion_risk: float
    confidence: float
    label: str
    summary: str
    features: FeatureVector
    evidence: list[Evidence]
    missing: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


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
    def create(
        cls,
        kind: str,
        title: str,
        detail: str = "",
        tool: str | None = None,
        status: Literal["working", "done", "warning", "error"] = "done",
    ) -> "RunEvent":
        return cls(at=utcnow_iso(), kind=kind, title=title, detail=detail, tool=tool, status=status)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)
