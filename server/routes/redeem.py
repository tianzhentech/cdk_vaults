"""
CDK Vaults — 公开兑换路由
POST /api/redeem          普通 CDK 兑换 (文本/链接/普通文件)
POST /api/redeem/codex    Codex 批量兑换 + 格式转换下载
"""

import os
import io
import json
import re
import secrets
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from server.models import RedeemRequest, RedeemResponse, AssetResponse, CodexRedeemRequest
from server.database import get_db_context, get_redeem_notice
from server.utils.codex_converter import (
    cpa_access_token,
    cpa_has_text_passwords,
    cpa_to_auth_json,
    cpa_to_sub2api_account,
    cpa_to_text_line,
    wrap_sub2api,
)

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # → server/
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DOWNLOAD_TOKEN_TTL_MINUTES = 15

# ── 内置分类名称 ──────────────────────────────────────
CODEX_CATEGORY_NAME = "Codex"
EXPORT_TZ = ZoneInfo("Asia/Shanghai")


@router.get("/notice")
def get_public_notice():
    """兑换页公开通知。"""
    with get_db_context() as db:
        notice = get_redeem_notice(db)
    content = notice["content"].strip()
    return {
        "enabled": bool(notice["enabled"] and content),
        "content": content if notice["enabled"] else "",
    }


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


def _cdk_category_name(db, cdk, asset=None):
    category_id = _cdk_category_id(db, cdk, asset)
    if category_id is None:
        return None
    cat = db.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    return cat["name"] if cat else None


def _cdk_category_id(db, cdk, asset=None):
    category_id = cdk["category_id"] if "category_id" in cdk.keys() else None
    if category_id is None and asset is not None:
        category_id = asset["category_id"]
    if category_id is None and cdk["asset_id"]:
        row = db.execute("SELECT category_id FROM assets WHERE id = ?", (cdk["asset_id"],)).fetchone()
        category_id = row["category_id"] if row else None
    return category_id


def _category_asset_clause(category_id):
    if category_id is None:
        return "a.category_id IS NULL", []
    return "a.category_id = ?", [category_id]


def _validate_cdk_record(db, code: str, allow_used: bool = False):
    """验证 CDK 基础状态，返回 cdk_row。"""
    cdk = db.execute("SELECT * FROM cdk_codes WHERE code = ?", (code,)).fetchone()
    if not cdk:
        raise HTTPException(status_code=404, detail=f"兑换码 {code} 不存在")

    if cdk["status"] == "used":
        if allow_used:
            return cdk
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

    return cdk


def _redeemed_assets_for_cdk(db, cdk):
    return db.execute(
        """
        SELECT a.*, MIN(src.sort_id) AS redeemed_sort
        FROM (
            SELECT rl.asset_id, rl.id AS sort_id
            FROM redemption_logs rl
            WHERE rl.cdk_id = ?
            UNION ALL
            SELECT ca.asset_id, ca.id AS sort_id
            FROM cdk_assets ca
            WHERE ca.cdk_id = ? AND ca.consumed_at IS NOT NULL
        ) src
        JOIN assets a ON a.id = src.asset_id
        GROUP BY a.id
        ORDER BY redeemed_sort ASC, a.id ASC
        """,
        (cdk["id"], cdk["id"]),
    ).fetchall()


def _available_inventory_assets(db, cdk, limit: int | None = None):
    category_id = _cdk_category_id(db, cdk)
    category_clause, category_params = _category_asset_clause(category_id)
    limit_clause = ""
    params = [*category_params]
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(limit)
    return db.execute(
        f"""
        SELECT a.*
        FROM assets a
        WHERE {category_clause}
          AND a.consumed_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM cdk_assets ca
              WHERE ca.asset_id = a.id AND ca.consumed_at IS NOT NULL
          )
        ORDER BY a.created_at ASC, a.id ASC
        {limit_clause}
        """,
        params,
    ).fetchall()


def _cdk_counts(db, cdk) -> dict:
    used_from_assets = db.execute(
        "SELECT COUNT(*) FROM cdk_assets WHERE cdk_id = ? AND consumed_at IS NOT NULL",
        (cdk["id"],),
    ).fetchone()[0]
    used_from_logs = db.execute(
        "SELECT COUNT(*) FROM redemption_logs WHERE cdk_id = ?",
        (cdk["id"],),
    ).fetchone()[0]
    used = max(int(cdk["used_count"] or 0), used_from_assets, used_from_logs)
    category_id = _cdk_category_id(db, cdk)
    category_clause, category_params = _category_asset_clause(category_id)
    inventory = db.execute(
        f"""SELECT COUNT(*)
            FROM assets a
            WHERE {category_clause}
              AND a.consumed_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM cdk_assets ca
                  WHERE ca.asset_id = a.id AND ca.consumed_at IS NOT NULL
              )""",
        category_params,
    ).fetchone()[0]
    quota_total = max(int(cdk["max_uses"] or 1), used)
    remaining = max(quota_total - used, 0)
    return {
        "total": quota_total,
        "assigned": used,
        "available": inventory,
        "inventory": inventory,
        "unassigned_inventory": inventory,
        "used": used,
        "remaining": remaining,
    }


def _select_assets_for_redeem(db, cdk, requested_count: int, code: str):
    counts = _cdk_counts(db, cdk)
    if counts["remaining"] <= 0:
        db.execute("UPDATE cdk_codes SET status = 'used' WHERE id = ?", (cdk["id"],))
        raise HTTPException(status_code=400, detail=f"兑换码 {code} 已无可兑换资产")
    if requested_count > counts["remaining"]:
        raise HTTPException(status_code=400, detail=f"本次数量超过兑换码剩余额度，仅剩 {counts['remaining']} 个")

    assets = _available_inventory_assets(db, cdk, requested_count)
    if not assets:
        raise HTTPException(
            status_code=400,
            detail=f"兑换码 {code} 暂无可兑换库存，理论剩余 {counts['remaining']} / {counts['total']} 个",
        )
    if requested_count > len(assets):
        raise HTTPException(status_code=400, detail=f"兑换码 {code} 当前库存不足，仅剩 {len(assets)} 个")
    return assets, counts


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
    assets, _ = _select_assets_for_redeem(db, cdk, 1, code)
    return cdk, assets[0]


def _consume_cdk(db, cdk, asset, request: Request):
    """消费资产并记到 CDK：资产只在兑换瞬间归属某个 CDK。"""
    now = datetime.now(timezone.utc).isoformat()
    asset_cursor = db.execute(
        """UPDATE assets
           SET consumed_at = ?, consumed_by_cdk_id = ?
           WHERE id = ? AND consumed_at IS NULL""",
        (now, cdk["id"], asset["id"]),
    )
    if asset_cursor.rowcount != 1:
        raise HTTPException(status_code=409, detail="该资产已被消耗，请重试")

    db.execute("DELETE FROM cdk_assets WHERE asset_id = ? AND consumed_at IS NULL", (asset["id"],))
    try:
        db.execute(
            "INSERT INTO cdk_assets (cdk_id, asset_id, consumed_at) VALUES (?, ?, ?)",
            (cdk["id"], asset["id"], now),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="该资产已被消耗，请重试") from exc

    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    prior_logs = db.execute(
        "SELECT COUNT(*) FROM redemption_logs WHERE cdk_id = ?",
        (cdk["id"],),
    ).fetchone()[0]
    if prior_logs == 0:
        db.execute("UPDATE cdk_codes SET asset_id = ? WHERE id = ?", (asset["id"], cdk["id"]))
    db.execute(
        "INSERT INTO redemption_logs (cdk_id, asset_id, ip_address, user_agent) VALUES (?, ?, ?, ?)",
        (cdk["id"], asset["id"], ip, ua),
    )
    used_count = db.execute(
        "SELECT COUNT(*) FROM redemption_logs WHERE cdk_id = ?",
        (cdk["id"],),
    ).fetchone()[0]
    total_count = max(int(cdk["max_uses"] or 1), used_count)
    remaining_count = max(total_count - used_count, 0)
    new_status = "used" if remaining_count == 0 else "active"
    db.execute(
        "UPDATE cdk_codes SET used_count = ?, status = ? WHERE id = ?",
        (used_count, new_status, cdk["id"]),
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


def _codex_asset_label(asset, cpa: dict) -> str:
    return str(cpa.get("email") or asset["name"] or "unknown").strip()


def _validate_codex_payload_format(cpa: dict, fmt: str, code: str, asset) -> None:
    has_access_token = bool(cpa_access_token(cpa))
    has_passwords = cpa_has_text_passwords(cpa)
    label = _codex_asset_label(asset, cpa)

    if not has_access_token and fmt != "text":
        raise HTTPException(
            status_code=400,
            detail=f"{label} 缺少 access_token，只能兑换为文本格式",
        )
    if has_access_token and not has_passwords and fmt == "text":
        raise HTTPException(
            status_code=400,
            detail=f"{label} 缺少 GPT密码/邮箱密码字段，只能兑换为 CPA、Sub2API 或 auth.json 格式",
        )


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
            cdk = _validate_cdk_record(db, code, allow_used=True)
        except HTTPException:
            return {"found": False, "category_name": None, "is_codex": False}
        counts = _cdk_counts(db, cdk)
        redeemed_assets = _redeemed_assets_for_cdk(db, cdk)
        asset = redeemed_assets[0] if redeemed_assets else None
        cat_name = _cdk_category_name(db, cdk, asset)
        is_single_use_cdk = int(cdk["max_uses"] or 1) <= 1
        already_redeemed = (
            is_single_use_cdk
            and counts["used"] > 0
            and counts["remaining"] <= 0
            and len(redeemed_assets) > 0
        )
        return {
            "found": True,
            "category_name": cat_name,
            "is_codex": cat_name == CODEX_CATEGORY_NAME,
            "already_redeemed": already_redeemed,
            "reexport_count": len(redeemed_assets) if already_redeemed else 0,
            "remaining_count": counts["remaining"],
            "inventory_count": counts["inventory"],
            "used_count": counts["used"],
            "total_count": counts["total"],
        }


# ── 普通兑换 ──────────────────────────────────────────
@router.post("", response_model=RedeemResponse)
def redeem_cdk(body: RedeemRequest, request: Request):
    """普通 CDK 兑换 — 文本/链接/文件资产"""
    code = body.code.strip().upper()

    with get_db_context("IMMEDIATE") as db:
        cdk = _validate_cdk_record(db, code)
        available_assets, _ = _select_assets_for_redeem(db, cdk, body.quantity, code)

        redeemed_assets = []
        for asset in available_assets:
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
        inventory_count=counts["inventory"],
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
    reexported = False
    allow_reexport = len(codes) == 1

    with get_db_context("IMMEDIATE") as db:
        for code in codes:
            cdk = _validate_cdk_record(db, code, allow_used=True)
            counts = _cdk_counts(db, cdk)
            redeemed_assets = _redeemed_assets_for_cdk(db, cdk)
            is_single_use_cdk = int(cdk["max_uses"] or 1) <= 1
            should_reexport = (
                allow_reexport
                and is_single_use_cdk
                and counts["remaining"] <= 0
                and len(redeemed_assets) > 0
            )
            if should_reexport:
                assets = redeemed_assets
                reexported = True
            else:
                cdk = _validate_cdk_record(db, code)
                assets, _ = _select_assets_for_redeem(db, cdk, body.quantity, code)

            for asset in assets:
                # 验证是否属于 Codex 分类
                if asset["category_id"]:
                    cat = db.execute("SELECT name FROM categories WHERE id = ?", (asset["category_id"],)).fetchone()
                    if not cat or cat["name"] != CODEX_CATEGORY_NAME:
                        raise HTTPException(status_code=400, detail=f"兑换码 {code} 对应的资产不属于 Codex 分类")
                else:
                    raise HTTPException(status_code=400, detail=f"兑换码 {code} 对应的资产不属于 Codex 分类")

                cpa = _load_cpa_json(asset)
                _validate_codex_payload_format(cpa, fmt, code, asset)
                if not should_reexport:
                    _consume_cdk(db, cdk, asset, request)
                cpa_items.append((cdk, asset, cpa))
        cdk_by_id = {cdk["id"]: cdk for cdk, _, _ in cpa_items}
        cdk_counts = [_cdk_counts(db, cdk) for cdk in cdk_by_id.values()]
        remaining_count = sum(counts["remaining"] for counts in cdk_counts)
        inventory_count = sum(counts["inventory"] for counts in cdk_counts)

    # ── 生成下载 ──────────────────────────────────
    response_headers = {
        "X-Redeemed-Count": str(len(cpa_items)),
        "X-Remaining-Count": str(remaining_count),
        "X-Inventory-Count": str(inventory_count),
        "X-Reexported": "1" if reexported else "0",
    }
    if fmt == "cpa":
        return _export_cpa(cpa_items, response_headers)
    elif fmt == "sub2api_single":
        return _export_sub2api_single(cpa_items, response_headers)
    elif fmt in ("auth_json", "sub2api_multi"):
        return _export_auth_json(cpa_items, response_headers)
    elif fmt == "text":
        return _export_text(cpa_items, response_headers)


def _with_headers(headers: dict, disposition: str) -> dict:
    return {**headers, "Content-Disposition": disposition}


def _export_date_suffix() -> str:
    return datetime.now(EXPORT_TZ).strftime("%m%d_%H%M")


def _safe_filename_part(value, fallback: str = "unknown") -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[^A-Za-z0-9._@+]+", "_", text)
    text = text.strip("._")
    return text or fallback


def _json_filename(prefix: str, name, date_suffix: str) -> str:
    return f"{prefix}_{_safe_filename_part(name)}_{date_suffix}.json"


def _zip_filename(base: str, date_suffix: str, count: int) -> str:
    return f"{base}_{date_suffix}_{count}.zip"


def _sub2api_all_filename(date_suffix: str, count: int) -> str:
    return f"sub2api_all_in_one_{date_suffix}_{count}.json"


def _auth_json_pack_filename(date_suffix: str, count: int) -> str:
    return f"auth_json_pack_{date_suffix}_{count}.zip"


def _text_filename(date_suffix: str, count: int) -> str:
    return f"codex_accounts_{date_suffix}_{count}.txt"


def _export_cpa(items: list, headers: dict | None = None) -> StreamingResponse:
    """CPA 格式: 单个=JSON，多个=ZIP"""
    headers = headers or {}
    date_suffix = _export_date_suffix()
    if len(items) == 1:
        _, asset, cpa = items[0]
        content = json.dumps(cpa, indent=2, ensure_ascii=False)
        email = cpa.get("email", "codex")
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="application/json",
            headers=_with_headers(headers, f'attachment; filename="{_json_filename("codex", email, date_suffix)}"'),
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, asset, cpa in items:
            email = cpa.get("email", "unknown")
            fname = _json_filename("codex", email, date_suffix)
            zf.writestr(fname, json.dumps(cpa, indent=2, ensure_ascii=False))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers=_with_headers(headers, f'attachment; filename="{_zip_filename("codex_cpa_pack", date_suffix, len(items))}"'),
    )


def _export_sub2api_single(items: list, headers: dict | None = None) -> StreamingResponse:
    """Sub2API 单文件: 所有账号合并到一个 JSON"""
    headers = headers or {}
    date_suffix = _export_date_suffix()
    accounts = [cpa_to_sub2api_account(cpa) for _, _, cpa in items]
    export = wrap_sub2api(accounts)
    content = json.dumps(export, indent=2, ensure_ascii=False)
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="application/json",
        headers=_with_headers(headers, f'attachment; filename="{_sub2api_all_filename(date_suffix, len(items))}"'),
    )


def _export_auth_json(items: list, headers: dict | None = None) -> StreamingResponse:
    """Codex auth.json 格式: 单个=auth.json，多个=ZIP，每个账号一个 auth.json。"""
    headers = headers or {}
    date_suffix = _export_date_suffix()
    converted = []
    for _, asset, cpa in items:
        email = cpa.get("email", "unknown")
        auth_json = cpa_to_auth_json(cpa)
        folder = _safe_filename_part(email or asset["name"], "unknown")
        converted.append((f"{folder}/auth.json", json.dumps(auth_json, indent=2, ensure_ascii=False)))

    if len(converted) == 1:
        _, content = converted[0]
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="application/json",
            headers=_with_headers(headers, 'attachment; filename="auth.json"'),
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, content in converted:
            zf.writestr(fname, content)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers=_with_headers(headers, f'attachment; filename="{_auth_json_pack_filename(date_suffix, len(converted))}"'),
    )


def _export_text(items: list, headers: dict | None = None) -> JSONResponse:
    """文本格式: 邮箱----GPT密码----邮箱密码，一行一个。"""
    headers = headers or {}
    date_suffix = _export_date_suffix()
    text = "\n".join(cpa_to_text_line(cpa) for _, _, cpa in items)
    return JSONResponse(
        {
            "success": True,
            "format": "text",
            "filename": _text_filename(date_suffix, len(items)),
            "text": text,
            "redeemed_count": len(items),
            "remaining_count": int(headers.get("X-Remaining-Count", 0)),
            "inventory_count": int(headers.get("X-Inventory-Count", 0)),
            "reexported": headers.get("X-Reexported") == "1",
        },
        headers=headers,
    )
