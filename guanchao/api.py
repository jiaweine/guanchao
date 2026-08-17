from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api_core import create_app as create_core_app


def _case_id_from_path(path: str) -> str | None:
    parts = [part for part in path.split('/') if part]
    if len(parts) < 3 or parts[0] != 'api' or parts[1] != 'cases' or parts[2] == 'batch':
        return None
    return parts[2]


def _request_member(app: FastAPI, request: Request) -> dict[str, Any] | None:
    trust_actor_header = os.getenv('GUANCHAO_TRUST_ACTOR_HEADER', '0').strip().lower() in {'1', 'true', 'yes'}
    member_id = 'local'
    if trust_actor_header:
        member_id = (request.headers.get('X-Guanchao-Actor') or 'local').strip()[:64]
    try:
        return app.state.store.get_member(member_id)
    except KeyError:
        return None


def create_app(db_path: str | None = None) -> FastAPI:
    app = create_core_app(db_path)

    @app.middleware('http')
    async def enforce_product_state_consistency(request: Request, call_next: Any):
        path = request.url.path
        method = request.method.upper()
        case_id = _case_id_from_path(path)
        is_target_refresh = bool(case_id and method == 'PATCH' and path.endswith('/target'))
        is_asset_upload = bool(case_id and method == 'POST' and path.endswith('/assets'))
        is_asset_delete = bool(case_id and method == 'DELETE' and '/assets/' in path)
        is_asset_mutation = is_asset_upload or is_asset_delete
        needs_guard = is_target_refresh or is_asset_mutation

        member = _request_member(app, request) if needs_guard else None
        can_manage = bool(member and member.get('role') in {'admin', 'analyst'})

        if can_manage and case_id:
            try:
                case = app.state.store.get_case(case_id)
            except KeyError:
                case = None
            if case and case.get('status') == 'archived':
                return JSONResponse(
                    status_code=409,
                    content={'detail': '已归档调查不能修改资料或素材，请先恢复'},
                )
            if case and is_asset_mutation and app.state.store.active_run_for_case(case_id):
                return JSONResponse(
                    status_code=409,
                    content={'detail': '当前核查仍在进行，请完成后再修改素材，避免本轮证据快照发生歧义'},
                )

        response = await call_next(request)

        if can_manage and case_id and is_target_refresh and response.status_code == 429:
            try:
                case = app.state.store.get_case(case_id)
            except KeyError:
                return response
            return JSONResponse(
                status_code=202,
                content={
                    'case': case,
                    'run_id': None,
                    'capacity_limited': True,
                },
                headers={'X-Guanchao-Run-Deferred': '1'},
            )
        return response

    return app
