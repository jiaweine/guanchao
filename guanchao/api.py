from __future__ import annotations

import asyncio
import os
import time
import weakref
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .api_core import create_app as create_core_app

IDEMPOTENCY_TTL_SECONDS = 600.0
IDEMPOTENCY_CACHE_LIMIT = 256


def _case_id_from_path(path: str) -> str | None:
    parts = [part for part in path.split('/') if part]
    if len(parts) < 3 or parts[0] != 'api' or parts[1] != 'cases' or parts[2] == 'batch':
        return None
    return parts[2]


def _actor_identity(request: Request) -> str:
    trust_actor_header = os.getenv('GUANCHAO_TRUST_ACTOR_HEADER', '0').strip().lower() in {'1', 'true', 'yes'}
    if trust_actor_header:
        return (request.headers.get('X-Guanchao-Actor') or 'local').strip()[:64]
    return 'local'


def _request_member(app: FastAPI, request: Request) -> dict[str, Any] | None:
    try:
        return app.state.store.get_member(_actor_identity(request))
    except KeyError:
        return None


def _serialize_case_mutation(path: str, method: str, case_id: str | None) -> bool:
    if not case_id or method in {'GET', 'HEAD', 'OPTIONS'}:
        return False
    if path.endswith('/comments'):
        return False
    return True


def _idempotency_scope(request: Request) -> str | None:
    if request.method.upper() != 'POST' or request.url.path not in {'/api/cases', '/api/cases/batch'}:
        return None
    key = (request.headers.get('X-Guanchao-Request-Key') or '').strip()[:160]
    if not key:
        return None
    return f"{_actor_identity(request)}:{request.url.path}:{key}"


def _response_from_record(record: tuple[int, dict[str, str], bytes]) -> Response:
    status, headers, body = record
    return Response(content=body, status_code=status, headers=headers)


async def _record_response(response: Response) -> tuple[int, dict[str, str], bytes]:
    body = b''.join([chunk async for chunk in response.body_iterator])
    return response.status_code, dict(response.headers), body


def create_app(db_path: str | None = None) -> FastAPI:
    app = create_core_app(db_path)
    case_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
    idempotency_guard = asyncio.Lock()
    idempotency_cache: OrderedDict[str, tuple[float, tuple[int, dict[str, str], bytes]]] = OrderedDict()
    idempotency_inflight: dict[str, asyncio.Future[tuple[int, dict[str, str], bytes] | None]] = {}

    def lock_for(case_id: str) -> asyncio.Lock:
        lock = case_locks.get(case_id)
        if lock is None:
            lock = asyncio.Lock()
            case_locks[case_id] = lock
        return lock

    def prune_idempotency_cache(now: float) -> None:
        expired = [key for key, (created, _) in idempotency_cache.items() if now - created > IDEMPOTENCY_TTL_SECONDS]
        for key in expired:
            idempotency_cache.pop(key, None)
        while len(idempotency_cache) > IDEMPOTENCY_CACHE_LIMIT:
            idempotency_cache.popitem(last=False)

    async def idempotent_call(scope: str, action: Any) -> Response:
        while True:
            owner = False
            async with idempotency_guard:
                now = time.monotonic()
                prune_idempotency_cache(now)
                cached = idempotency_cache.get(scope)
                if cached:
                    idempotency_cache.move_to_end(scope)
                    return _response_from_record(cached[1])
                future = idempotency_inflight.get(scope)
                if future is None:
                    future = asyncio.get_running_loop().create_future()
                    idempotency_inflight[scope] = future
                    owner = True
            if owner:
                break
            record = await future
            if record is not None:
                return _response_from_record(record)

        try:
            response = await action()
            record = await _record_response(response)
        except BaseException:
            async with idempotency_guard:
                waiter = idempotency_inflight.pop(scope, None)
                if waiter and not waiter.done():
                    waiter.set_result(None)
            raise

        async with idempotency_guard:
            if 200 <= record[0] < 300:
                idempotency_cache[scope] = (time.monotonic(), record)
                idempotency_cache.move_to_end(scope)
                prune_idempotency_cache(time.monotonic())
            waiter = idempotency_inflight.pop(scope, None)
            if waiter and not waiter.done():
                waiter.set_result(record)
        return _response_from_record(record)

    @app.middleware('http')
    async def enforce_product_state_consistency(request: Request, call_next: Any):
        path = request.url.path
        method = request.method.upper()
        case_id = _case_id_from_path(path)

        async def dispatch():
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

        async def serialized_dispatch():
            if _serialize_case_mutation(path, method, case_id):
                async with lock_for(case_id):
                    return await dispatch()
            return await dispatch()

        scope = _idempotency_scope(request)
        if scope:
            await request.body()
            return await idempotent_call(scope, serialized_dispatch)
        return await serialized_dispatch()

    return app
