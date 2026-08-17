from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .domain import AccountSnapshot
from .evolution import EvolutionEngine
from .harness import ActiveRunError, AgentHarness
from .multimodal import PerceptionGateway, infer_kind
from .post_training import PostTrainingCorpusBuilder
from .sample_data import demo_target
from .store import Store

MAX_UPLOAD_BYTES = int(os.getenv("GUANCHAO_MAX_UPLOAD_MB", "30")) * 1024 * 1024


class CaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=1000)
    targets: list[dict[str, Any]] = Field(min_length=1, max_length=30)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ReviewCreate(BaseModel):
    case_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    decision: Literal["confirm_ordinary", "uncertain", "confirm_marketing"]
    reason: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=1000)
    reviewer: str = Field(default="local", max_length=80)


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.getenv("GUANCHAO_DB", "guanchao.db"))
    harness = AgentHarness(store)
    perception = PerceptionGateway()
    app = FastAPI(title="Guanchao", docs_url="/docs", redoc_url=None)
    app.state.store = store
    app.state.harness = harness
    app.state.perception = perception

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "ok": True,
            "name": "观潮",
            "inputs": ["text", "image", "video", "audio", "document"],
            **store.product_metrics(),
        }

    @app.get("/api/cases")
    def list_cases() -> list[dict[str, Any]]:
        return store.list_cases()

    @app.post("/api/cases")
    def create_case(payload: CaseCreate) -> dict[str, Any]:
        try:
            targets = [AccountSnapshot.from_dict(raw).asdict() for raw in payload.targets]
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(422, f"账号资料格式不正确：{exc}") from exc
        return store.create_case(payload.title.strip(), payload.goal.strip(), targets)

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str) -> dict[str, Any]:
        try:
            return store.get_case(case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @app.delete("/api/cases/{case_id}")
    def delete_case(case_id: str) -> dict[str, bool]:
        if store.active_run_for_case(case_id):
            raise HTTPException(409, "核查进行中，暂时不能删除调查")
        try:
            store.delete_case(case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        return {"ok": True}

    @app.post("/api/cases/{case_id}/assets")
    async def upload_asset(case_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
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
            asset = store.create_asset(case_id, filename, kind, content_type, len(data), storage_path)
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
    def delete_asset(case_id: str, asset_id: str) -> dict[str, bool]:
        if store.active_run_for_case(case_id):
            raise HTTPException(409, "核查进行中，暂时不能删除素材")
        try:
            store.delete_asset(case_id, asset_id)
        except KeyError as exc:
            raise HTTPException(404, "素材不存在") from exc
        return {"ok": True}

    @app.post("/api/cases/{case_id}/messages")
    def send_message(case_id: str, payload: MessageCreate) -> dict[str, str]:
        try:
            store.get_case(case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        try:
            return {"run_id": harness.start(case_id, payload.content.strip())}
        except ActiveRunError as exc:
            raise HTTPException(409, f"当前任务已有核查在进行：{exc}") from exc

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            return store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "执行记录不存在") from exc

    @app.get("/api/review-queue")
    def review_queue(reviewed: bool | None = False) -> list[dict[str, Any]]:
        return store.review_queue(reviewed=reviewed)

    @app.post("/api/reviews")
    def review(payload: ReviewCreate) -> dict[str, Any]:
        try:
            item = store.add_review(
                payload.case_id,
                payload.run_id,
                payload.decision,
                payload.reason,
                payload.note,
                payload.reviewer,
            )
        except KeyError as exc:
            raise HTTPException(404, "执行记录不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "review": item}

    @app.post("/api/evolution/run")
    def evolve() -> dict[str, Any]:
        engine = EvolutionEngine()
        report = engine.evolve(
            store.get_calibration(), store.labeled_examples(), store.get_policy_profile()
        )
        if report.accepted:
            store.save_calibration(report.calibration)
            store.save_policy_profile(report.policy_profile)
        return report.to_dict()

    @app.get("/api/post-training/export", response_class=PlainTextResponse)
    def export_post_training() -> str:
        cases = [store.get_case(item["id"]) for item in store.list_cases()]
        return PostTrainingCorpusBuilder().build_jsonl(cases, store.review_rows())

    @app.post("/api/demo")
    def demo() -> dict[str, Any]:
        target = demo_target()
        case = store.create_case(
            "橙子生活研究所 · 内容复核",
            "帮我判断这个账号是不是长期营销运营号。不要只看一条内容，要自己核查并给出证据。",
            [target],
        )
        return {"case": case, "run_id": harness.start(case["id"], case["goal"])}

    frontend = Path(__file__).resolve().parent.parent / "frontend"
    if frontend.exists():
        app.mount("/assets", StaticFiles(directory=frontend), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(frontend / "index.html")

    return app
