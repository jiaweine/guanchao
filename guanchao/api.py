from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .domain import AccountSnapshot
from .evolution import EvolutionEngine
from .harness import ActiveRunError, AgentHarness, RunCapacityError
from .multimodal import PerceptionGateway, infer_kind
from .post_training import PostTrainingCorpusBuilder
from .reporting import ReportBuilder
from .sample_data import demo_target
from .store import Store

MAX_UPLOAD_BYTES = int(os.getenv("GUANCHAO_MAX_UPLOAD_MB", "30")) * 1024 * 1024


class CaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=1000)
    targets: list[dict[str, Any]] = Field(min_length=1, max_length=30)
    owner: str | None = Field(default=None, min_length=1, max_length=64)
    priority: Literal["low", "normal", "high"] = "normal"
    tags: list[str] = Field(default_factory=list, max_length=12)


class BatchCreate(BaseModel):
    title: str = Field(default="批量调查", min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=1000)
    targets: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    owner: str | None = Field(default=None, min_length=1, max_length=64)
    priority: Literal["low", "normal", "high"] = "normal"
    tags: list[str] = Field(default_factory=list, max_length=12)
    auto_start: bool = True


class CasePatch(BaseModel):
    owner: str | None = Field(default=None, min_length=1, max_length=64)
    priority: Literal["low", "normal", "high"] | None = None
    tags: list[str] | None = Field(default=None, max_length=12)
    archived: bool | None = None
    monitoring_enabled: bool | None = None
    monitoring_interval_hours: int | None = Field(default=None, ge=1, le=24 * 365)


class TargetUpdate(BaseModel):
    target: dict[str, Any]
    rerun: bool = True
    message: str = Field(default="根据刚更新的账号资料重新核查，并指出与上一轮相比发生了什么变化。", max_length=2000)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class CommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ReviewCreate(BaseModel):
    case_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    decision: Literal["confirm_ordinary", "uncertain", "confirm_marketing"]
    reason: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=1000)


class MemberCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=80)
    role: Literal["admin", "analyst", "reviewer"]


class WorkspaceSettings(BaseModel):
    retention_days: int = Field(default=0, ge=0, le=3650)


class EventCreate(BaseModel):
    event_type: Literal["case_opened"]
    case_id: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.getenv("GUANCHAO_DB", "guanchao.db"))
    harness = AgentHarness(store)
    perception = PerceptionGateway()
    store.purge_retention(actor="system")
    app = FastAPI(title="Guanchao", docs_url="/docs", redoc_url=None)
    app.state.store = store
    app.state.harness = harness
    app.state.perception = perception
    trust_actor_header = os.getenv("GUANCHAO_TRUST_ACTOR_HEADER", "0").strip().lower() in {"1", "true", "yes"}

    def actor(request: Request) -> dict[str, Any]:
        member_id = "local"
        if trust_actor_header:
            member_id = (request.headers.get("X-Guanchao-Actor") or "local").strip()[:64]
        try:
            return store.get_member(member_id)
        except KeyError as exc:
            raise HTTPException(403, "当前成员不可用") from exc

    def require(request: Request, allowed: set[str]) -> dict[str, Any]:
        member = actor(request)
        if member["role"] not in allowed:
            raise HTTPException(403, "当前角色没有执行此操作的权限")
        return member

    def normalize_targets(raw_targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            return [AccountSnapshot.from_dict(raw).asdict() for raw in raw_targets]
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(422, f"账号资料格式不正确：{exc}") from exc

    @app.get("/healthz")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/status")
    def status(request: Request) -> dict[str, Any]:
        actor(request)
        return {
            "ok": True,
            "name": "观潮",
            "inputs": ["text", "image", "video", "audio", "document"],
            **store.product_metrics(),
        }

    @app.get("/api/metrics")
    def metrics(request: Request) -> dict[str, Any]:
        actor(request)
        return store.product_metrics()

    @app.get("/api/session")
    def session(request: Request) -> dict[str, Any]:
        return actor(request)

    @app.get("/api/members")
    def members(request: Request) -> list[dict[str, Any]]:
        actor(request)
        return store.list_members()

    @app.post("/api/members")
    def save_member(payload: MemberCreate, request: Request) -> dict[str, Any]:
        current = require(request, {"admin"})
        try:
            return store.save_member(payload.id, payload.display_name, payload.role, actor=current["id"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/members/{member_id}")
    def deactivate_member(member_id: str, request: Request) -> dict[str, bool]:
        current = require(request, {"admin"})
        try:
            store.deactivate_member(member_id, actor=current["id"])
        except KeyError as exc:
            raise HTTPException(404, "成员不存在") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/workspace/settings")
    def workspace_settings(request: Request) -> dict[str, Any]:
        actor(request)
        return store.workspace_settings()

    @app.put("/api/workspace/settings")
    def update_workspace_settings(payload: WorkspaceSettings, request: Request) -> dict[str, Any]:
        current = require(request, {"admin"})
        return store.save_workspace_settings(payload.retention_days, actor=current["id"])

    @app.post("/api/workspace/purge")
    def purge_workspace(request: Request) -> dict[str, Any]:
        current = require(request, {"admin"})
        return store.purge_retention(actor=current["id"])

    @app.get("/api/audit")
    def audit(request: Request, case_id: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        actor(request)
        return store.audit_events(case_id=case_id, limit=limit)

    @app.post("/api/events")
    def record_product_event(payload: EventCreate, request: Request) -> dict[str, bool]:
        current = actor(request)
        try:
            store.get_case(payload.case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        store.record_event(
            payload.event_type,
            actor=current["id"],
            case_id=payload.case_id,
            run_id=payload.run_id,
        )
        return {"ok": True}

    @app.get("/api/cases")
    def list_cases(
        request: Request,
        query: str = "",
        platform: str = "",
        status: Literal["open", "archived", "all"] = "open",
        owner: str = "",
        priority: Literal["", "low", "normal", "high"] = "",
        sort: Literal["updated_desc", "updated_asc", "created_desc", "risk_desc"] = "updated_desc",
        batch_id: str = "",
    ) -> list[dict[str, Any]]:
        actor(request)
        return store.list_cases(query, platform, status, owner, priority, sort, batch_id)

    @app.post("/api/cases")
    def create_case(payload: CaseCreate, request: Request) -> dict[str, Any]:
        current = require(request, {"admin", "analyst"})
        targets = normalize_targets(payload.targets)
        try:
            return store.create_case(
                payload.title.strip(),
                payload.goal.strip(),
                targets,
                owner=payload.owner or current["id"],
                priority=payload.priority,
                tags=payload.tags,
                actor=current["id"],
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, "负责人或优先级设置不正确") from exc

    @app.post("/api/cases/batch")
    def create_batch(payload: BatchCreate, request: Request) -> dict[str, Any]:
        current = require(request, {"admin", "analyst"})
        targets = normalize_targets(payload.targets)
        try:
            batch = store.create_batch(
                payload.title.strip(),
                payload.goal.strip(),
                targets,
                owner=payload.owner or current["id"],
                priority=payload.priority,
                tags=payload.tags,
                actor=current["id"],
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, "批量调查设置不正确") from exc
        runs: list[dict[str, str]] = []
        capacity_limited = False
        if payload.auto_start:
            try:
                runs = harness.start_many(
                    [item["id"] for item in batch["cases"]], payload.goal.strip(), actor=current["id"]
                )
            except RunCapacityError:
                capacity_limited = True
        return {
            "batch": {key: value for key, value in batch.items() if key != "cases"},
            "cases": batch["cases"],
            "runs": runs,
            "capacity_limited": capacity_limited,
        }

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str, request: Request) -> dict[str, Any]:
        actor(request)
        try:
            return store.get_case(case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @app.patch("/api/cases/{case_id}")
    def patch_case(case_id: str, payload: CasePatch, request: Request) -> dict[str, Any]:
        current = require(request, {"admin", "analyst"})
        if payload.archived is True and store.active_run_for_case(case_id):
            raise HTTPException(409, "核查进行中，暂时不能归档")
        try:
            return store.update_case(
                case_id,
                owner=payload.owner,
                priority=payload.priority,
                tags=payload.tags,
                archived=payload.archived,
                monitoring_enabled=payload.monitoring_enabled,
                monitoring_interval_hours=payload.monitoring_interval_hours,
                actor=current["id"],
            )
        except KeyError as exc:
            raise HTTPException(404, "任务或负责人不存在") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/cases/{case_id}/target")
    def refresh_target(case_id: str, payload: TargetUpdate, request: Request) -> dict[str, Any]:
        current = require(request, {"admin", "analyst"})
        if store.active_run_for_case(case_id):
            raise HTTPException(409, "当前核查仍在进行，请完成后再更新资料")
        target = normalize_targets([payload.target])[0]
        try:
            case = store.update_target(case_id, target, actor=current["id"])
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        run_id = None
        if payload.rerun:
            try:
                run_id = harness.start(case_id, payload.message.strip(), actor=current["id"])
            except RunCapacityError as exc:
                raise HTTPException(429, "当前核查队列已满，请稍后重新发起") from exc
        return {"case": case, "run_id": run_id}

    @app.delete("/api/cases/{case_id}")
    def delete_case(case_id: str, request: Request) -> dict[str, bool]:
        current = require(request, {"admin"})
        if store.active_run_for_case(case_id):
            raise HTTPException(409, "核查进行中，暂时不能删除调查")
        try:
            store.delete_case(case_id, actor=current["id"])
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        return {"ok": True}

    @app.post("/api/cases/{case_id}/assets")
    async def upload_asset(case_id: str, request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        current = require(request, {"admin", "analyst"})
        try:
            store.get_case(case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

        filename = Path(file.filename or "asset").name[:180]
        content_type = file.content_type or "application/octet-stream"
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if not data:
            raise HTTPException(422, "素材内容为空")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "单个素材超过上传上限")

        kind = infer_kind(content_type, filename)
        store.asset_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix[:12]
        with tempfile.NamedTemporaryFile(
            prefix=f"{case_id}-", suffix=suffix, dir=store.asset_dir, delete=False
        ) as handle:
            handle.write(data)
            storage_path = handle.name

        try:
            asset = store.create_asset(
                case_id, filename, kind, content_type, len(data), storage_path, actor=current["id"]
            )
        except Exception:
            Path(storage_path).unlink(missing_ok=True)
            raise

        try:
            extracted, asset_status = perception.extract(storage_path, kind, content_type)
            note = (
                "已读取"
                if asset_status == "ready"
                else "已保存，等待本地感知服务"
                if asset_status == "pending"
                else "解析失败"
            )
            store.update_asset(
                asset.id,
                asset_status,
                extracted_text=extracted,
                note=note,
                error="" if asset_status != "error" else "perception_failed",
            )
        except Exception:
            store.update_asset(
                asset.id,
                "error",
                note="解析失败，可重新上传或检查本地感知服务",
                error="perception_failed",
            )
        return store.get_asset(asset.id, include_text=False)

    @app.delete("/api/cases/{case_id}/assets/{asset_id}")
    def delete_asset(case_id: str, asset_id: str, request: Request) -> dict[str, bool]:
        current = require(request, {"admin", "analyst"})
        if store.active_run_for_case(case_id):
            raise HTTPException(409, "核查进行中，暂时不能删除素材")
        try:
            store.delete_asset(case_id, asset_id, actor=current["id"])
        except KeyError as exc:
            raise HTTPException(404, "素材不存在") from exc
        return {"ok": True}

    @app.post("/api/cases/{case_id}/comments")
    def add_comment(case_id: str, payload: CommentCreate, request: Request) -> dict[str, Any]:
        current = actor(request)
        try:
            item = store.add_comment(case_id, current["id"], payload.content)
        except KeyError as exc:
            raise HTTPException(404, "任务或成员不存在") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "comment": item}

    @app.post("/api/cases/{case_id}/messages")
    def send_message(case_id: str, payload: MessageCreate, request: Request) -> dict[str, str]:
        current = require(request, {"admin", "analyst"})
        try:
            case = store.get_case(case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        if case["status"] == "archived":
            raise HTTPException(409, "已归档调查不能继续执行，请先恢复")
        try:
            return {"run_id": harness.start(case_id, payload.content.strip(), actor=current["id"])}
        except ActiveRunError as exc:
            raise HTTPException(409, f"当前任务已有核查在进行：{exc}") from exc
        except RunCapacityError as exc:
            raise HTTPException(429, "当前核查队列已满，请稍后重新发起") from exc

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict[str, Any]:
        actor(request)
        try:
            return store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "执行记录不存在") from exc

    @app.get("/api/review-queue")
    def review_queue(
        request: Request,
        reviewed: bool | None = False,
        query: str = "",
        platform: str = "",
        owner: str = "",
        priority: Literal["", "low", "normal", "high"] = "",
        sort: Literal["priority_desc", "risk_desc", "newest"] = "priority_desc",
    ) -> list[dict[str, Any]]:
        actor(request)
        return store.review_queue(reviewed, query, platform, owner, priority, sort)

    @app.get("/api/monitoring")
    def monitoring_queue(request: Request, due_only: bool = True) -> list[dict[str, Any]]:
        actor(request)
        return store.monitoring_queue(due_only=due_only)

    @app.post("/api/reviews")
    def review(payload: ReviewCreate, request: Request) -> dict[str, Any]:
        current = require(request, {"admin", "analyst", "reviewer"})
        try:
            item = store.add_review(
                payload.case_id,
                payload.run_id,
                payload.decision,
                payload.reason,
                payload.note,
                current["id"],
            )
        except KeyError as exc:
            raise HTTPException(404, "执行记录或复核成员不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "review": item}

    @app.get("/api/cases/{case_id}/report")
    def report(case_id: str, request: Request, output: Literal["markdown", "json"] = "markdown") -> Response:
        actor(request)
        try:
            case = store.get_case(case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        if output == "json":
            return JSONResponse(ReportBuilder.build_payload(case))
        text = ReportBuilder.build_markdown(case)
        return Response(
            content=text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="guanchao-{case_id}.md"'},
        )

    @app.post("/api/evolution/run")
    def evolve(request: Request) -> dict[str, Any]:
        current = require(request, {"admin"})
        engine = EvolutionEngine()
        report = engine.evolve(
            store.get_calibration(), store.labeled_examples(), store.get_policy_profile()
        )
        if report.accepted:
            store.save_calibration(report.calibration)
            store.save_policy_profile(report.policy_profile)
        store.record_event(
            "learning_run",
            actor=current["id"],
            metadata={"accepted": report.accepted, "examples": report.examples},
        )
        return report.to_dict()

    @app.get("/api/post-training/export", response_class=PlainTextResponse)
    def export_post_training(request: Request) -> str:
        require(request, {"admin"})
        cases = [store.get_case(item["id"]) for item in store.list_cases(status="all")]
        return PostTrainingCorpusBuilder().build_jsonl(cases, store.review_rows())

    @app.post("/api/demo")
    def demo(request: Request) -> dict[str, Any]:
        current = require(request, {"admin", "analyst"})
        target = demo_target()
        case = store.create_case(
            "橙子生活研究所 · 内容复核",
            "帮我判断这个账号是不是长期营销运营号。不要只看一条内容，要自己核查并给出证据。",
            [target],
            owner=current["id"],
            actor=current["id"],
        )
        try:
            run_id = harness.start(case["id"], case["goal"], actor=current["id"])
        except RunCapacityError as exc:
            raise HTTPException(429, "当前核查队列已满，请稍后重新发起") from exc
        return {"case": case, "run_id": run_id}

    frontend = Path(__file__).resolve().parent.parent / "frontend"
    if frontend.exists():
        app.mount("/assets", StaticFiles(directory=frontend), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(frontend / "index.html")

    return app
