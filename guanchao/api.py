from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .api_core import MAX_UPLOAD_BYTES, create_app as create_core_app
from .multimodal import infer_kind

IDEMPOTENCY_TTL_SECONDS = 600.0
IDEMPOTENCY_CACHE_LIMIT = 256


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


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


def _idempotency_base(request: Request) -> str | None:
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


async def _await_durable_task(task: asyncio.Task[Any]) -> Any:
    """Let a committed side-effect finish even when the client request is cancelled."""
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            interrupted = True
            continue
    if interrupted:
        raise asyncio.CancelledError
    return result


def _persist_asset_file(asset_dir: Path, case_id: str, filename: str, data: bytes) -> str:
    asset_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix[:12]
    with tempfile.NamedTemporaryFile(
        prefix=f'{case_id}-', suffix=suffix, dir=asset_dir, delete=False
    ) as handle:
        handle.write(data)
        return handle.name


def create_app(db_path: str | None = None) -> FastAPI:
    app = create_core_app(db_path)
    case_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
    idempotency_guard = asyncio.Lock()
    idempotency_cache: OrderedDict[
        str, tuple[float, str, tuple[int, dict[str, str], bytes]]
    ] = OrderedDict()
    idempotency_inflight: dict[
        str, tuple[str, asyncio.Future[tuple[int, dict[str, str], bytes] | None]]
    ] = {}
    perception_slots = asyncio.Semaphore(
        _env_int('GUANCHAO_PERCEPTION_WORKERS', 4, 1, 64)
    )

    def lock_for(case_id: str) -> asyncio.Lock:
        lock = case_locks.get(case_id)
        if lock is None:
            lock = asyncio.Lock()
            case_locks[case_id] = lock
        return lock

    def prune_idempotency_cache(now: float) -> None:
        expired = [key for key, (created, _, _) in idempotency_cache.items() if now - created > IDEMPOTENCY_TTL_SECONDS]
        for key in expired:
            idempotency_cache.pop(key, None)
        while len(idempotency_cache) > IDEMPOTENCY_CACHE_LIMIT:
            idempotency_cache.popitem(last=False)

    async def idempotent_call(base: str, digest: str, action: Any) -> Response:
        while True:
            owner = False
            async with idempotency_guard:
                now = time.monotonic()
                prune_idempotency_cache(now)
                cached = idempotency_cache.get(base)
                if cached:
                    if cached[1] != digest:
                        return JSONResponse(
                            status_code=409,
                            content={'detail': '同一个请求标识不能提交不同的调查内容'},
                        )
                    idempotency_cache.move_to_end(base)
                    return _response_from_record(cached[2])
                inflight = idempotency_inflight.get(base)
                if inflight is None:
                    future = asyncio.get_running_loop().create_future()
                    idempotency_inflight[base] = (digest, future)
                    owner = True
                else:
                    inflight_digest, future = inflight
                    if inflight_digest != digest:
                        return JSONResponse(
                            status_code=409,
                            content={'detail': '同一个请求标识不能提交不同的调查内容'},
                        )
            if owner:
                break
            # All duplicate callers share this future. A disconnected/cancelled
            # waiter must not cancel the shared synchronization primitive and
            # poison every other retry waiting on the same idempotency key.
            record = await asyncio.shield(future)
            if record is not None:
                return _response_from_record(record)

        try:
            response = await action()
            record = await _record_response(response)
        except BaseException:
            async with idempotency_guard:
                inflight = idempotency_inflight.pop(base, None)
                if inflight and not inflight[1].done():
                    inflight[1].set_result(None)
            raise

        async with idempotency_guard:
            if 200 <= record[0] < 300:
                idempotency_cache[base] = (time.monotonic(), digest, record)
                idempotency_cache.move_to_end(base)
                prune_idempotency_cache(time.monotonic())
            inflight = idempotency_inflight.pop(base, None)
            if inflight and not inflight[1].done():
                inflight[1].set_result(record)
        return _response_from_record(record)

    async def upload_asset_without_blocking_loop(
        request: Request,
        case_id: str,
        actor_id: str,
    ) -> Response:
        form = await request.form()
        file = form.get('file')
        if file is None or not hasattr(file, 'read'):
            return JSONResponse(status_code=422, content={'detail': '缺少素材文件'})

        filename = Path(getattr(file, 'filename', '') or 'asset').name[:180]
        content_type = getattr(file, 'content_type', None) or 'application/octet-stream'
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if not data:
            return JSONResponse(status_code=422, content={'detail': '素材内容为空'})
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse(status_code=413, content={'detail': '单个素材超过上传上限'})

        kind = infer_kind(content_type, filename)
        storage_path = await asyncio.to_thread(
            _persist_asset_file, app.state.store.asset_dir, case_id, filename, data
        )
        try:
            asset = await asyncio.to_thread(
                app.state.store.create_asset,
                case_id,
                filename,
                kind,
                content_type,
                len(data),
                storage_path,
                actor_id,
            )
        except Exception:
            await asyncio.to_thread(Path(storage_path).unlink, missing_ok=True)
            raise

        async def settle_asset() -> dict[str, Any]:
            try:
                async with perception_slots:
                    extracted, asset_status = await asyncio.to_thread(
                        app.state.perception.extract, storage_path, kind, content_type
                    )
                note = (
                    '已读取'
                    if asset_status == 'ready'
                    else '已保存，等待本地感知服务'
                    if asset_status == 'pending'
                    else '解析失败'
                )
                await asyncio.to_thread(
                    app.state.store.update_asset,
                    asset.id,
                    asset_status,
                    extracted,
                    '' if asset_status != 'error' else 'perception_failed',
                    note,
                )
            except Exception:
                await asyncio.to_thread(
                    app.state.store.update_asset,
                    asset.id,
                    'error',
                    '',
                    'perception_failed',
                    '解析失败，可重新上传或检查本地感知服务',
                )
            return await asyncio.to_thread(app.state.store.get_asset, asset.id, False)

        settlement = asyncio.create_task(settle_asset(), name=f'guanchao-asset-{asset.id}')
        public_asset = await _await_durable_task(settlement)
        return JSONResponse(content=public_asset)

    @app.middleware('http')
    async def enforce_product_state_consistency(request: Request, call_next: Any):
        path = request.url.path
        method = request.method.upper()
        case_id = _case_id_from_path(path)
        review_feedback: dict[str, Any] | None = None
        if method == 'POST' and path == '/api/reviews':
            try:
                raw = json.loads((await request.body()).decode('utf-8'))
                if isinstance(raw, dict):
                    review_feedback = raw
            except (UnicodeDecodeError, json.JSONDecodeError):
                review_feedback = None

        async def dispatch():
            is_target_refresh = bool(case_id and method == 'PATCH' and path.endswith('/target'))
            is_asset_upload = bool(case_id and method == 'POST' and path.endswith('/assets'))
            is_asset_delete = bool(case_id and method == 'DELETE' and '/assets/' in path)
            is_asset_mutation = is_asset_upload or is_asset_delete
            needs_guard = is_target_refresh or is_asset_mutation

            member = _request_member(app, request) if needs_guard else None
            can_manage = bool(member and member.get('role') in {'admin', 'analyst'})

            case = None
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

            if is_asset_upload and can_manage and case_id and case:
                return await upload_asset_without_blocking_loop(request, case_id, member['id'])

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

        base = _idempotency_base(request)
        if base:
            body = await request.body()
            digest = hashlib.sha256(body).hexdigest()
            response = await idempotent_call(base, digest, serialized_dispatch)
        else:
            response = await serialized_dispatch()

        if review_feedback and 200 <= response.status_code < 300:
            run_id = str(review_feedback.get('run_id') or '')
            decision = str(review_feedback.get('decision') or '')
            if run_id and decision in {'confirm_ordinary', 'uncertain', 'confirm_marketing'}:
                app.state.harness.observe_review(run_id, decision, _actor_identity(request))
        return response

    return app
