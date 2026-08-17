from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .domain import AccountSnapshot
from .evolution import EvolutionEngine
from .harness import AgentHarness
from .sample_data import demo_target
from .store import Store


class CaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=1000)
    targets: list[dict[str, Any]] = Field(min_length=1, max_length=30)


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class FeedbackCreate(BaseModel):
    case_id: str
    label: int = Field(ge=0, le=1)
    note: str = Field(default="", max_length=1000)


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.getenv("GUANCHAO_DB", "guanchao.db"))
    harness = AgentHarness(store)
    app = FastAPI(title="Guanchao", docs_url="/docs", redoc_url=None)
    app.state.store = store
    app.state.harness = harness

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        cases = store.list_cases()
        return {"ok": True, "name": "观潮", "cases": len(cases), "feedback": len(store.labeled_examples())}

    @app.get("/api/cases")
    def list_cases() -> list[dict[str, Any]]:
        return store.list_cases()

    @app.post("/api/cases")
    def create_case(payload: CaseCreate) -> dict[str, Any]:
        try:
            targets = [AccountSnapshot.from_dict(raw).asdict() for raw in payload.targets]
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"账号资料格式不正确：{exc}") from exc
        return store.create_case(payload.title.strip(), payload.goal.strip(), targets)

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str) -> dict[str, Any]:
        try:
            return store.get_case(case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @app.post("/api/cases/{case_id}/messages")
    def send_message(case_id: str, payload: MessageCreate) -> dict[str, Any]:
        try:
            store.get_case(case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        return {"run_id": harness.start(case_id, payload.content.strip())}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            return store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "执行记录不存在") from exc

    @app.post("/api/feedback")
    def feedback(payload: FeedbackCreate) -> dict[str, Any]:
        try:
            case = store.get_case(payload.case_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        runs = case.get("runs") or []
        completed = next((r for r in runs if r["status"] == "completed" and r["state"].get("primary_result")), None)
        if not completed:
            raise HTTPException(409, "请先完成一次调查再提交复核")
        features = completed["state"]["primary_result"].get("features")
        if not features:
            raise HTTPException(409, "这次调查没有可用于复核的特征快照")
        item = store.add_feedback(payload.case_id, payload.label, features, payload.note)
        return {"ok": True, "feedback_id": item["id"]}

    @app.post("/api/evolution/run")
    def evolve() -> dict[str, Any]:
        engine = EvolutionEngine()
        current = store.get_calibration()
        report = engine.evolve(current, store.labeled_examples())
        if report.accepted:
            store.save_calibration(report.calibration)
        return report.to_dict()

    @app.post("/api/demo")
    def demo() -> dict[str, Any]:
        target = demo_target()
        case = store.create_case(
            "橙子生活研究所 · 营销倾向复核",
            "帮我判断这个账号是不是长期营销运营号。不要只看一条内容，要自己核查并给出证据。",
            [target],
        )
        run_id = harness.start(case["id"], case["goal"])
        return {"case": case, "run_id": run_id}

    frontend = Path(__file__).resolve().parent.parent / "frontend"
    if frontend.exists():
        app.mount("/assets", StaticFiles(directory=frontend), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(frontend / "index.html")

    return app

