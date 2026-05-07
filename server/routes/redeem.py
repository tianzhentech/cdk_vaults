"""
CDK Vaults — 公开兑换路由
POST /api/redeem          普通 CDK 兑换 (文本/链接/普通文件)
POST /api/redeem/codex    Codex 批量兑换 + 格式转换下载
"""

import os
import io
import json
import secrets
import zipfile
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from server.models import RedeemRequest, RedeemResponse, AssetResponse, CodexRedeemRequest
from server.database import get_db_context
from server.utils.codex_converter import cpa_to_sub2api_account, wrap_sub2api

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # → server/
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DOWNLOAD_TOKEN_TTL_MINUTES = 15

# ── 内置分类名称 ──────────────────────────────────────
CODEX_CATEGORY_NAME = "Codex"


def _resolve_asset_file_path(asset) -> str:
    """解析内部上传文件路径，不允许越过 uploads 目录。"""
    fp = asset["file_path"]
    if not fp:
        raise HTTPException(status_code=400, detail=f"资产 {asset['name']} 无文件")
    full_path = os.path.abspath(os.path.join(BASE_DIR, fp.lstrip("/")))
    upload_root = os.path.abspath(UPLOAD_DIR)
    if not full_path.startswith(upload_root + os.sep):
        raise HTTPException(status_code=500, detail=f"资产文件路径无效: {asset['name']}")
    if not os.path.exists(full_path):
        raise HTTPException(status_code=500, detail=f"资产文件不存在: {asset['name']}")
    return full_path


def _ensure_cdk_asset_rows(db, cdk):
    """兼容旧数据：没有资产明细时，用旧 asset_id 补一条。"""
    exists = db.execute("SELECT 1 FROM cdk_assets WHERE cdk_id = ? LIMIT 1", (cdk["id"],)).fetchone()
    if not exists and cdk["asset_id"]:
        db.execute(
            "INSERT OR IGNORE INTO cdk_assets (cdk_id, asset_id) VALUES (?, ?)",
            (cdk["id"], cdk["asset_id"]),
        )


def _validate_cdk_record(db, code: str):
    """验证 CDK 基础状态，返回 cdk_row。"""
    cdk = db.execute("SELECT * FROM cdk_codes WHERE code = ?", (code,)).fetchone()
    if not cdk:
        raise HTTPException(status_code=404, detail=f"兑换码 {code} 不存在")

    if cdk["status"] == "used":
        raise HTTPException(status_code=400, detail=f"兑换码 {code} 已被使用")
    if cdk["status"] == "disabled":
        raise HTTPException(status_code=400, detail=f"兑换码 {code} 已被禁用")
    if cdk["status"] == "expired":
        raise HTTPException(status_code=400, detail=f"兑换码 {code} 已过期")

    # 检查过期
    if cdk["expires_at"]:
        try:
            expires = datetime.fromisoformat(cdk["expires_at"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires:
                db.execute("UPDATE cdk_codes SET status = 'expired' WHERE id = ?", (cdk["id"],))
                raise HTTPException(status_code=400, detail=f"兑换码 {code} 已过期")
        except ValueError:
            pass

    _ensure_cdk_asset_rows(db, cdk)
    return cdk


def _available_cdk_assets(db, cdk):
    return db.execute(
        """
        SELECT ca.id AS cdk_asset_item_id, a.*
        FROM cdk_assets ca
        JOIN assets a ON a.id = ca.asset_id
        WHERE ca.cdk_id = ?
          AND ca.consumed_at IS NULL
          AND a.consumed_at IS NULL
        ORDER BY ca.id ASC
        """,
        (cdk["id"],),
    ).fetchall()


def _cdk_counts(db, cdk) -> dict:
    total = db.execute(
        "SELECT COUNT(*) FROM cdk_assets WHERE cdk_id = ?",
        (cdk["id"],),
    ).fetchone()[0]
    used = db.execute(
        "SELECT COUNT(*) FROM cdk_assets WHERE cdk_id = ? AND consumed_at IS NOT NULL",
        (cdk["id"],),
    ).fetchone()[0]
    remaining = db.execute(
        """SELECT COUNT(*)
           FROM cdk_assets ca
           JOIN assets a ON a.id = ca.asset_id
           WHERE ca.cdk_id = ? AND ca.consumed_at IS NULL AND a.consumed_at IS NULL""",
        (cdk["id"],),
    ).fetchone()[0]
    return {"total": total, "used": used, "remaining": remaining}


def _asset_category_name(db, asset):
    if not asset["category_id"]:
        return None
    cat = db.execute("SELECT name FROM categories WHERE id = ?", (asset["category_id"],)).fetchone()
    return cat["name"] if cat else None


def _asset_response(asset, category_name=None, download_url=None) -> AssetResponse:
    return AssetResponse(
        id=asset["id"],
        name=asset["name"],
        type=asset["type"],
        description=asset["description"] or "",
        file_path=None if asset["type"] == "file" else asset["file_path"],
        download_url=download_url,
        content=asset["content"],
        category_id=asset["category_id"],
        category_name=category_name,
        thumbnail=asset["thumbnail"],
        created_at=asset["created_at"],
        updated_at=asset["updated_at"],
    )


def _validate_cdk(db, code: str, request: Request) -> tuple:
    """验证单个 CDK，返回 (cdk_row, asset_row)，验证失败抛异常"""
    cdk = _validate_cdk_record(db, code)
    assets = _available_cdk_assets(db, cdk)
    if not assets:
        db.execute("UPDATE cdk_codes SET status = 'used' WHERE id = ?", (cdk["id"],))
        raise HTTPException(status_code=400, detail=f"兑换码 {code} 已无可兑换资产")

    return cdk, assets[0]


def _consume_cdk(db, cdk, asset, request: Request):
    """消费 CDK：更新次数 + 写日志"""
    now = datetime.now(timezone.utc).isoformat()
    item_id = asset["cdk_asset_item_id"]
    item_cursor = db.execute(
        "UPDATE cdk_assets SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
        (now, item_id),
    )
    if item_cursor.rowcount != 1:
        raise HTTPException(status_code=409, detail="该资产已被消耗，请重试")

    asset_cursor = db.execute(
        """UPDATE assets
           SET consumed_at = ?, consumed_by_cdk_id = ?
           WHERE id = ? AND consumed_at IS NULL""",
        (now, cdk["id"], asset["id"]),
    )
    if asset_cursor.rowcount != 1:
        raise HTTPException(status_code=409, detail="该资产已被消耗，请重试")

    used_count = db.execute(
        "SELECT COUNT(*) FROM cdk_assets WHERE cdk_id = ? AND consumed_at IS NOT NULL",
        (cdk["id"],),
    ).fetchone()[0]
    remaining_count = db.execute(
        """SELECT COUNT(*)
           FROM cdk_assets ca
           JOIN assets a ON a.id = ca.asset_id
           WHERE ca.cdk_id = ? AND ca.consumed_at IS NULL AND a.consumed_at IS NULL""",
        (cdk["id"],),
    ).fetchone()[0]
    new_status = "used" if remaining_count == 0 else "active"
    db.execute(
        "UPDATE cdk_codes SET used_count = ?, status = ? WHERE id = ?",
        (used_count, new_status, cdk["id"]),
    )
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    db.execute(
        "INSERT INTO redemption_logs (cdk_id, asset_id, ip_address, user_agent) VALUES (?, ?, ?, ?)",
        (cdk["id"], asset["id"], ip, ua),
    )
    return {"used_count": used_count, "remaining_count": remaining_count, "status": new_status}


def _create_download_token(db, cdk, asset) -> str:
    """为文件资产创建一次性下载地址。"""
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=DOWNLOAD_TOKEN_TTL_MINUTES)).isoformat()
    db.execute(
        """INSERT INTO file_download_tokens (token, cdk_id, asset_id, expires_at)
           VALUES (?, ?, ?, ?)""",
        (token, cdk["id"], asset["id"], expires_at),
    )
    return f"/api/redeem/download/{token}"


def _load_cpa_json(asset) -> dict:
    """从文件资产加载 CPA JSON"""
    full_path = _resolve_asset_file_path(asset)
    with open(full_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── CDK 探测 (不消费) ─────────────────────────────────
@router.post("/detect")
def detect_cdk(body: RedeemRequest):
    """
    探测 CDK 所属分类 — 不消费，仅返回分类信息。
    前端用来动态切换 UI（普通兑换 vs Codex 导出格式选择）。
    """
    code = body.code.strip().upper()
    with get_db_context() as db:
        try:
            cdk = _validate_cdk_record(db, code)
        except HTTPException:
            return {"found": False, "category_name": None, "is_codex": False}
        assets = _available_cdk_assets(db, cdk)
        if not assets:
            return {"found": False, "category_name": None, "is_codex": False}
        asset = assets[0]
        counts = _cdk_counts(db, cdk)
        if not asset["category_id"]:
            cat_name = None
        else:
            cat_name = _asset_category_name(db, asset)
        return {
            "found": True,
            "category_name": cat_name,
            "is_codex": cat_name == CODEX_CATEGORY_NAME,
            "remaining_count": counts["remaining"],
            "used_count": counts["used"],
            "total_count": counts["total"],
        }


# ── 普通兑换 ──────────────────────────────────────────
@router.post("", response_model=RedeemResponse)
def redeem_cdk(body: RedeemRequest, request: Request):
    """普通 CDK 兑换 — 文本/链接/文件资产"""
    code = body.code.strip().upper()

    with get_db_context() as db:
        cdk = _validate_cdk_record(db, code)
        available_assets = _available_cdk_assets(db, cdk)
        if not available_assets:
            db.execute("UPDATE cdk_codes SET status = 'used' WHERE id = ?", (cdk["id"],))
            raise HTTPException(status_code=400, detail=f"兑换码 {code} 已无可兑换资产")
        if body.quantity > len(available_assets):
            raise HTTPException(status_code=400, detail=f"剩余可兑换资产不足，仅剩 {len(available_assets)} 个")

        redeemed_assets = []
        for asset in available_assets[:body.quantity]:
            _consume_cdk(db, cdk, asset, request)
            download_url = _create_download_token(db, cdk, asset) if asset["type"] == "file" else None
            redeemed_assets.append(_asset_response(asset, _asset_category_name(db, asset), download_url))

        counts = _cdk_counts(db, cdk)

    return RedeemResponse(
        success=True,
        message=f"兑换成功，共兑换 {len(redeemed_assets)} 个资产",
        asset=redeemed_assets[0] if redeemed_assets else None,
        assets=redeemed_assets,
        redeemed_count=len(redeemed_assets),
        remaining_count=counts["remaining"],
        total_count=counts["total"],
    )


@router.get("/download/{token}")
def download_redeemed_file(token: str):
    """一次性受控下载文件资产。下载 token 用过或过期后不可再次使用。"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as db:
        cursor = db.execute(
            """UPDATE file_download_tokens
               SET used_at = ?
               WHERE token = ? AND used_at IS NULL AND expires_at > ?""",
            (now, token, now),
        )
        if cursor.rowcount != 1:
            row = db.execute(
                "SELECT used_at, expires_at FROM file_download_tokens WHERE token = ?",
                (token,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="下载链接不存在")
            if row["used_at"]:
                raise HTTPException(status_code=410, detail="下载链接已使用")
            raise HTTPException(status_code=410, detail="下载链接已过期")

        asset = db.execute(
            """SELECT a.*
               FROM file_download_tokens t
               JOIN assets a ON a.id = t.asset_id
               WHERE t.token = ?""",
            (token,),
        ).fetchone()
        if not asset or asset["type"] != "file":
            raise HTTPException(status_code=404, detail="资产文件不存在")

    full_path = _resolve_asset_file_path(asset)
    return FileResponse(full_path, filename=asset["name"], media_type="application/octet-stream")


# ── Codex 批量兑换 ────────────────────────────────────
@router.post("/codex")
def redeem_codex(body: CodexRedeemRequest, request: Request):
    """Codex 专用批量兑换 — 支持 CPA / Sub2API 格式导出"""
    codes = [c.strip().upper() for c in body.codes if c.strip()]
    if not codes:
        raise HTTPException(status_code=400, detail="请输入至少一个兑换码")
    seen = set()
    for code in codes:
        if code in seen:
            raise HTTPException(status_code=400, detail=f"兑换码 {code} 重复提交")
        seen.add(code)

    fmt = body.format
    cpa_items = []

    with get_db_context() as db:
        for code in codes:
            cdk = _validate_cdk_record(db, code)
            assets = _available_cdk_assets(db, cdk)
            if not assets:
                db.execute("UPDATE cdk_codes SET status = 'used' WHERE id = ?", (cdk["id"],))
                raise HTTPException(status_code=400, detail=f"兑换码 {code} 已无可兑换资产")
            if body.quantity > len(assets):
                raise HTTPException(status_code=400, detail=f"兑换码 {code} 剩余可兑换资产不足，仅剩 {len(assets)} 个")

            for asset in assets[:body.quantity]:
                # 验证是否属于 Codex 分类
                if asset["category_id"]:
                    cat = db.execute("SELECT name FROM categories WHERE id = ?", (asset["category_id"],)).fetchone()
                    if not cat or cat["name"] != CODEX_CATEGORY_NAME:
                        raise HTTPException(status_code=400, detail=f"兑换码 {code} 对应的资产不属于 Codex 分类")
                else:
                    raise HTTPException(status_code=400, detail=f"兑换码 {code} 对应的资产不属于 Codex 分类")

                cpa = _load_cpa_json(asset)
                cpa_items.append((cdk, asset, cpa))

        # 全部验证通过后才消费
        for cdk, asset, _ in cpa_items:
            _consume_cdk(db, cdk, asset, request)
        cdk_by_id = {cdk["id"]: cdk for cdk, _, _ in cpa_items}
        remaining_count = sum(_cdk_counts(db, cdk)["remaining"] for cdk in cdk_by_id.values())

    # ── 生成下载 ──────────────────────────────────
    response_headers = {
        "X-Redeemed-Count": str(len(cpa_items)),
        "X-Remaining-Count": str(remaining_count),
    }
    if fmt == "cpa":
        return _export_cpa(cpa_items, response_headers)
    elif fmt == "sub2api_single":
        return _export_sub2api_single(cpa_items, response_headers)
    elif fmt == "sub2api_multi":
        return _export_sub2api_multi(cpa_items, response_headers)


def _with_headers(headers: dict, disposition: str) -> dict:
    return {**headers, "Content-Disposition": disposition}


def _export_cpa(items: list, headers: dict | None = None) -> StreamingResponse:
    """CPA 格式: 单个=JSON，多个=ZIP"""
    headers = headers or {}
    if len(items) == 1:
        _, asset, cpa = items[0]
        content = json.dumps(cpa, indent=2, ensure_ascii=False)
        email = cpa.get("email", "codex")
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="application/json",
            headers=_with_headers(headers, f'attachment; filename="codex-{email}.json"'),
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, asset, cpa in items:
            email = cpa.get("email", "unknown")
            fname = f"codex-{email}.json"
            zf.writestr(fname, json.dumps(cpa, indent=2, ensure_ascii=False))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers=_with_headers(headers, 'attachment; filename="codex-cpa-pack.zip"'),
    )


def _export_sub2api_single(items: list, headers: dict | None = None) -> StreamingResponse:
    """Sub2API 单文件: 所有账号合并到一个 JSON"""
    headers = headers or {}
    accounts = [cpa_to_sub2api_account(cpa) for _, _, cpa in items]
    export = wrap_sub2api(accounts)
    content = json.dumps(export, indent=2, ensure_ascii=False)
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="application/json",
        headers=_with_headers(headers, 'attachment; filename="sub2api-all-in-one.json"'),
    )


def _export_sub2api_multi(items: list, headers: dict | None = None) -> StreamingResponse:
    """Sub2API 多文件: 每个账号单独一个 JSON，多个打 ZIP"""
    headers = headers or {}
    converted = []
    for _, asset, cpa in items:
        account = cpa_to_sub2api_account(cpa)
        export = wrap_sub2api([account])
        email = cpa.get("email", "unknown")
        fname = f"sub2api-{email}.json"
        converted.append((fname, json.dumps(export, indent=2, ensure_ascii=False)))

    if len(converted) == 1:
        fname, content = converted[0]
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="application/json",
            headers=_with_headers(headers, f'attachment; filename="{fname}"'),
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, content in converted:
            zf.writestr(fname, content)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers=_with_headers(headers, 'attachment; filename="sub2api-pack.zip"'),
    )
