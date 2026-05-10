"""
CDK Vaults — SSE 事件流
GET /api/events/public-stream  前台公开更新信号
GET /api/events/admin-stream   管理后台更新信号
"""

import asyncio
import json
import queue

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from server.auth import verify_admin_token
from server.event_bus import subscribe, unsubscribe

router = APIRouter()


def _sse_message(event: str, data: dict | None = None, event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(data or {}, ensure_ascii=False)
    lines.extend(f"data: {line}" for line in payload.splitlines())
    return "\n".join(lines) + "\n\n"


def _stream_headers() -> dict:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


async def _event_stream(request: Request, channel: str):
    subscriber = subscribe(channel)
    try:
        yield _sse_message("connected", {"channel": channel})
        while not await request.is_disconnected():
            try:
                message = await asyncio.to_thread(subscriber.get, True, 1)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            yield _sse_message(message["event"], message["data"], message["id"])
    finally:
        unsubscribe(channel, subscriber)


@router.get("/public-stream")
async def public_stream(request: Request):
    return StreamingResponse(
        _event_stream(request, "public"),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@router.get("/admin-stream")
async def admin_stream(request: Request, token: str = ""):
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Token")
    verify_admin_token(token)
    return StreamingResponse(
        _event_stream(request, "admin"),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )
