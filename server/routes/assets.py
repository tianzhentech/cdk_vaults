"""
CDK Vaults — 资产管理路由
GET    /api/assets          列出所有资产
POST   /api/assets          创建资产 (文本/链接)
POST   /api/assets/upload   上传文件资产
POST   /api/assets/upload-password 使用管理员密码上传文件资产
GET    /api/assets/{id}     获取资产详情
PUT    /api/assets/{id}     更新资产
DELETE /api/assets/{id}     删除资产
"""

import os
import uuid
import shutil
import sqlite3
import json as json_lib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from fastapi.responses import FileResponse
from server.models import AssetCreate, AssetUpdate, AssetRedeemStatusUpdate, AssetCodexExportRequest, AssetResponse
from server.auth import get_current_admin, verify_password
from server.database import get_db_context
from server.event_bus import publish_update
from server.routes import redeem as redeem_exports

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")


def redeemed_count_sql(alias: str = "a") -> str:
    return f"""
        MAX(
            (SELECT COUNT(*) FROM redemption_logs rl WHERE rl.asset_id = {alias}.id),
            CASE WHEN {alias}.consumed_at IS NOT NULL THEN 1 ELSE 0 END
        )
    """


def resolve_asset_file_path(file_path: str, must_exist: bool = True) -> str:
    """Resolve an internal upload path without allowing traversal outside uploads."""
    if not file_path:
        raise HTTPException(status_code=400, detail="资产无文件")
    full_path = os.path.abspath(os.path.join(BASE_DIR, file_path.lstrip("/")))
    upload_root = os.path.abspath(UPLOAD_DIR)
    if not full_path.startswith(upload_root + os.sep):
        raise HTTPException(status_code=500, detail="资产文件路径无效")
    if must_exist and not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="资产文件不存在")
    return full_path


def row_to_asset(row) -> dict:
    """将数据库行转为 AssetResponse 字典"""
    d = {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "description": row["description"] or "",
        "file_path": row["file_path"],
        "content": row["content"],
        "category_id": row["category_id"],
        "thumbnail": row["thumbnail"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    # JOIN 查询时可能包含 category_name
    try:
        d["category_name"] = row["category_name"]
    except (IndexError, KeyError):
        d["category_name"] = None
    try:
        d["redeemed_count"] = row["redeemed_count"]
    except (IndexError, KeyError):
        d["redeemed_count"] = 0
    d["cdk_binding_count"] = 0
    d["can_delete"] = d["redeemed_count"] == 0
    if d["redeemed_count"] > 0:
        d["delete_block_reason"] = f"资产已产生 {d['redeemed_count']} 条兑换记录，不能删除"
    else:
        d["delete_block_reason"] = None
    return d


def get_asset_delete_block(db, asset_id: int) -> str | None:
    redeemed_count = db.execute(
        """SELECT MAX(
               (SELECT COUNT(*) FROM redemption_logs WHERE asset_id = assets.id),
               CASE WHEN consumed_at IS NOT NULL THEN 1 ELSE 0 END
           )
           FROM assets WHERE id = ?""",
        (asset_id,),
    ).fetchone()[0]
    if redeemed_count:
        return f"资产已产生 {redeemed_count} 条兑换记录，不能删除"

    return None


def refresh_cdk_usage(db, cdk_ids):
    """Recalculate CDK counters after an admin manually changes asset redemption status."""
    for cdk_id in {int(cdk_id) for cdk_id in cdk_ids if cdk_id}:
        cdk = db.execute("SELECT * FROM cdk_codes WHERE id = ?", (cdk_id,)).fetchone()
        if not cdk:
            continue

        used_count = db.execute(
            """
            SELECT COUNT(DISTINCT asset_id)
            FROM (
                SELECT asset_id FROM redemption_logs WHERE cdk_id = ?
                UNION ALL
                SELECT asset_id FROM cdk_assets WHERE cdk_id = ? AND consumed_at IS NOT NULL
            )
            """,
            (cdk_id, cdk_id),
        ).fetchone()[0]
        primary_asset = db.execute(
            """
            SELECT asset_id
            FROM (
                SELECT asset_id, id AS sort_id FROM redemption_logs WHERE cdk_id = ?
                UNION ALL
                SELECT asset_id, id AS sort_id FROM cdk_assets WHERE cdk_id = ? AND consumed_at IS NOT NULL
            )
            GROUP BY asset_id
            ORDER BY MIN(sort_id) ASC, asset_id ASC
            LIMIT 1
            """,
            (cdk_id, cdk_id),
        ).fetchone()

        status = cdk["status"]
        if status not in ("disabled", "expired"):
            status = "used" if int(used_count or 0) >= int(cdk["max_uses"] or 1) else "active"

        db.execute(
            "UPDATE cdk_codes SET used_count = ?, asset_id = ?, status = ? WHERE id = ?",
            (
                int(used_count or 0),
                primary_asset["asset_id"] if primary_asset else None,
                status,
                cdk_id,
            ),
        )


def asset_with_usage(db, asset_id: int):
    return db.execute(f"""
        SELECT a.*, c.name as category_name,
               {redeemed_count_sql("a")} AS redeemed_count
        FROM assets a
        LEFT JOIN categories c ON a.category_id = c.id
        WHERE a.id = ?
    """, (asset_id,)).fetchone()


def codex_export_asset(db, asset_id: int):
    return db.execute("""
        SELECT a.*, c.name AS category_name
        FROM assets a
        LEFT JOIN categories c ON c.id = a.category_id
        WHERE a.id = ?
    """, (asset_id,)).fetchone()


def find_duplicate_asset(db, name: str, category_id: int | None, asset_type: str | None = None):
    params = [name]
    type_clause = ""
    if asset_type:
        type_clause = " AND a.type = ?"
        params.append(asset_type)
    params.extend([category_id, category_id])
    return db.execute(
        f"""
        SELECT a.*, c.name as category_name,
               {redeemed_count_sql("a")} AS redeemed_count
        FROM assets a
        LEFT JOIN categories c ON a.category_id = c.id
        WHERE a.name = ?{type_clause}
          AND (a.category_id = ? OR (a.category_id IS NULL AND ? IS NULL))
        LIMIT 1
        """,
        params,
    ).fetchone()


def asset_write_result(created_items=None, skipped_items=None) -> dict:
    created_items = created_items or []
    skipped_items = skipped_items or []
    return {
        "success": True,
        "created": len(created_items),
        "skipped": len(skipped_items),
        "items": created_items,
        "skipped_items": skipped_items,
    }


def asset_field(asset, key: str, default=None):
    if asset is None:
        return default
    if isinstance(asset, dict):
        return asset.get(key, default)
    try:
        return asset[key]
    except (KeyError, IndexError, TypeError):
        return getattr(asset, key, default)


def record_upload_log(
    db,
    *,
    asset=None,
    asset_id: int | None = None,
    asset_name: str = "",
    asset_type: str = "",
    category_id: int | None = None,
    source: str = "",
    original_filename: str = "",
    file_size: int = 0,
    status: str = "created",
    message: str = "",
):
    """记录资产新增/上传流水，便于后台追踪资产进入库存的来源。"""
    if asset is not None:
        asset_id = asset_field(asset, "id", asset_id)
        asset_name = asset_field(asset, "name", asset_name) or asset_name
        asset_type = asset_field(asset, "type", asset_type) or asset_type
        category_id = asset_field(asset, "category_id", category_id)

    db.execute(
        """INSERT INTO asset_upload_logs (
               asset_id, asset_name, asset_type, category_id, source,
               original_filename, file_size, status, message
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            asset_name or "",
            asset_type or "",
            category_id,
            source or "",
            original_filename or "",
            int(file_size or 0),
            status,
            message or "",
        ),
    )


def require_admin_password(password: str):
    if not verify_password(password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="管理员密码错误",
        )


def resolve_upload_category(db, category_id: int = 0, category_name: str = "") -> int | None:
    if category_id:
        row = db.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="指定的分类不存在")
        return row["id"]

    category_name = category_name.strip()
    if not category_name:
        return None

    row = db.execute("SELECT id FROM categories WHERE name = ?", (category_name,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"分类不存在: {category_name}")
    return row["id"]


def extract_upload_asset_name(file: UploadFile, raw: bytes, explicit_name: str = "") -> str:
    asset_name = explicit_name.strip()
    if asset_name:
        return asset_name

    asset_name = file.filename or "未命名资产"
    try:
        data = json_lib.loads(raw)
        if isinstance(data, dict) and data.get("email"):
            asset_name = str(data["email"]).strip()
    except Exception:
        pass

    return asset_name or file.filename or "未命名资产"


def save_file_asset(
    db,
    file: UploadFile,
    raw: bytes,
    asset_name: str,
    description: str,
    cat_id: int | None,
    *,
    source: str,
    duplicate_asset_type: str | None = "file",
):
    original_filename = file.filename or ""
    file_size = len(raw or b"")
    duplicate = find_duplicate_asset(db, asset_name, cat_id, duplicate_asset_type)
    if duplicate:
        skipped_item = row_to_asset(duplicate)
        record_upload_log(
            db,
            asset=skipped_item,
            source=source,
            original_filename=original_filename,
            file_size=file_size,
            status="skipped",
            message="重复资产，已跳过",
        )
        return None, skipped_item

    ext = os.path.splitext(file.filename)[1] if file.filename else ""
    unique_name = f"{uuid.uuid4().hex}{ext}"
    full_path = os.path.join(UPLOAD_DIR, unique_name)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(raw)

    relative_path = f"/uploads/{unique_name}"
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """INSERT INTO assets (name, type, description, file_path, category_id, created_at, updated_at)
           VALUES (?, 'file', ?, ?, ?, ?, ?)""",
        (asset_name, description, relative_path, cat_id, now, now),
    )
    asset_id = cursor.lastrowid
    row = db.execute("""
        SELECT a.*, c.name as category_name FROM assets a
        LEFT JOIN categories c ON a.category_id = c.id WHERE a.id = ?
    """, (asset_id,)).fetchone()
    created = row_to_asset(row)
    record_upload_log(
        db,
        asset=created,
        source=source,
        original_filename=original_filename,
        file_size=file_size,
        status="created",
        message="上传成功",
    )
    return created, None


@router.get("")
def list_assets(
    category_id: int = 0,
    search: str = "",
    page: int = 1,
    page_size: int = 20,
    _admin: str = Depends(get_current_admin),
):
    """列出资产 (分页)，支持分类和搜索过滤"""
    with get_db_context() as db:
        where = " WHERE 1=1"
        params = []
        if category_id:
            where += " AND a.category_id = ?"
            params.append(category_id)
        if search:
            where += " AND (a.name LIKE ? OR a.description LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])

        # 总数
        count_q = f"SELECT COUNT(*) FROM assets a{where}"
        total = db.execute(count_q, params).fetchone()[0]

        # 分页数据
        offset = (max(page, 1) - 1) * page_size
        data_q = f"""
            SELECT a.*, c.name as category_name,
                   {redeemed_count_sql("a")} AS redeemed_count
            FROM assets a
            LEFT JOIN categories c ON a.category_id = c.id
            {where}
            ORDER BY a.created_at DESC
            LIMIT ? OFFSET ?
        """
        rows = db.execute(data_q, params + [page_size, offset]).fetchall()

    pages = (total + page_size - 1) // page_size if total else 1
    return {
        "items": [row_to_asset(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post("/delete-batch")
def delete_assets_batch(
    body: dict,
    _admin: str = Depends(get_current_admin),
):
    """批量删除资产"""
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="请提供要删除的资产 ID 列表")

    deleted = 0
    blocked = []
    with get_db_context() as db:
        for aid in ids:
            row = db.execute("SELECT id, name, file_path FROM assets WHERE id = ?", (aid,)).fetchone()
            if not row:
                continue
            block_reason = get_asset_delete_block(db, aid)
            if block_reason:
                blocked.append({"id": aid, "name": row["name"], "reason": block_reason})
                continue
            db.execute("DELETE FROM assets WHERE id = ?", (aid,))
            if row["file_path"]:
                full_path = resolve_asset_file_path(row["file_path"], must_exist=False)
                if os.path.exists(full_path):
                    os.remove(full_path)
            deleted += 1

    if deleted:
        publish_update(["assets", "categories", "dashboard", "inventory"], audience="all")
    return {"success": True, "deleted": deleted, "blocked": blocked}


@router.post("")
def create_asset(body: AssetCreate, _admin: str = Depends(get_current_admin)):
    """创建文本或链接类型资产"""
    if body.type == "file":
        raise HTTPException(status_code=400, detail="文件资产请使用 /upload 接口")

    asset_name = body.name.strip()
    if not asset_name:
        raise HTTPException(status_code=400, detail="资产名称不能为空")
    cat_id = body.category_id if body.category_id else None

    with get_db_context() as db:
        duplicate = find_duplicate_asset(db, asset_name, cat_id, body.type)
        if duplicate:
            record_upload_log(
                db,
                asset=duplicate,
                asset_name=asset_name,
                asset_type=body.type,
                category_id=cat_id,
                source="manual_create",
                status="skipped",
                message="重复资产，已跳过",
            )
            publish_update(["upload_logs"], audience="admin")
            return asset_write_result(skipped_items=[row_to_asset(duplicate)])

        now = datetime.now(timezone.utc).isoformat()
        cursor = db.execute(
            """INSERT INTO assets (name, type, description, content, category_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (asset_name, body.type, body.description, body.content, cat_id, now, now),
        )
        asset_id = cursor.lastrowid
        row = db.execute("""
            SELECT a.*, c.name as category_name FROM assets a
            LEFT JOIN categories c ON a.category_id = c.id WHERE a.id = ?
        """, (asset_id,)).fetchone()
        created = row_to_asset(row)
        record_upload_log(
            db,
            asset=created,
            source="manual_create",
            status="created",
            message="创建成功",
        )
    publish_update(["assets", "categories", "dashboard", "inventory", "upload_logs"], audience="all")
    return asset_write_result(created_items=[created])


@router.post("/upload")
async def upload_asset(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(default=""),
    category_id: int = Form(default=0),
    _admin: str = Depends(get_current_admin),
):
    """上传文件资产"""
    asset_name = name.strip()
    if not asset_name:
        raise HTTPException(status_code=400, detail="资产名称不能为空")
    cat_id = category_id if category_id else None
    content = await file.read()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with get_db_context() as db:
        created, skipped = save_file_asset(
            db,
            file,
            content,
            asset_name,
            description,
            cat_id,
            source="single_upload",
        )
    if skipped:
        publish_update(["upload_logs"], audience="admin")
        return asset_write_result(skipped_items=[skipped])
    publish_update(["assets", "categories", "dashboard", "inventory", "upload_logs"], audience="all")
    return asset_write_result(created_items=[created])


@router.post("/upload-batch")
async def upload_batch(
    files: list[UploadFile] = File(...),
    category_id: int = Form(default=0),
    description: str = Form(default=""),
    _admin: str = Depends(get_current_admin),
):
    """
    批量上传文件资产 (Codex 专用)
    每个文件自动用 JSON 内的 email 字段作为资产名称，
    如果不是 JSON 则使用原始文件名。
    """
    import json as _json

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    cat_id = category_id if category_id else None
    results = []
    skipped = []

    with get_db_context() as db:
        for file in files:
            raw = await file.read()

            # 尝试从 JSON 中提取 email 作为资产名
            asset_name = file.filename or "未命名资产"
            try:
                data = _json.loads(raw)
                if isinstance(data, dict) and data.get("email"):
                    asset_name = str(data["email"]).strip()
            except Exception:
                pass
            if not asset_name:
                asset_name = file.filename or "未命名资产"

            created, skipped_item = save_file_asset(
                db,
                file,
                raw,
                asset_name,
                description,
                cat_id,
                source="batch_upload",
                duplicate_asset_type=None,
            )
            if skipped_item:
                skipped.append(skipped_item)
            elif created:
                results.append(created)

    if results:
        publish_update(["assets", "categories", "dashboard", "inventory", "upload_logs"], audience="all")
    elif skipped:
        publish_update(["upload_logs"], audience="admin")
    return asset_write_result(created_items=results, skipped_items=skipped)


@router.post("/upload-password")
async def upload_with_password(
    password: str = Form(...),
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
    category_id: int = Form(default=0),
    category_name: str = Form(default=""),
    description: str = Form(default=""),
    name: str = Form(default=""),
):
    """
    密码鉴权上传接口，便于脚本直接上传资产。

    multipart/form-data:
    - password: 管理员密码
    - file: 单个文件，或 files: 一个或多个文件
    - category_id 或 category_name: 可选分类
    - description: 可选描述
    - name: 可选资产名，仅上传单个文件时生效；未提供时 JSON 会用 email 字段命名
    """
    require_admin_password(password)
    upload_files = []
    if file:
        upload_files.append(file)
    if files:
        upload_files.extend(files)

    if not upload_files:
        raise HTTPException(status_code=400, detail="请提供要上传的文件")
    if name.strip() and len(upload_files) > 1:
        raise HTTPException(status_code=400, detail="批量上传时不能使用统一 name，请让系统按文件名或 JSON email 命名")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    results = []
    skipped = []

    with get_db_context(transaction_mode="IMMEDIATE") as db:
        cat_id = resolve_upload_category(db, category_id, category_name)
        for upload_file in upload_files:
            raw = await upload_file.read()
            asset_name = extract_upload_asset_name(upload_file, raw, name if len(upload_files) == 1 else "")
            created, skipped_item = save_file_asset(
                db,
                upload_file,
                raw,
                asset_name,
                description,
                cat_id,
                source="password_upload",
            )
            if skipped_item:
                skipped.append(skipped_item)
            elif created:
                results.append(created)

    if results:
        publish_update(["assets", "categories", "dashboard", "inventory", "upload_logs"], audience="all")
    elif skipped:
        publish_update(["upload_logs"], audience="admin")
    return asset_write_result(created_items=results, skipped_items=skipped)


@router.post("/export-codex")
def export_codex_assets(body: AssetCodexExportRequest, _admin: str = Depends(get_current_admin)):
    """管理员直接按前台 Codex 规则导出选中的文件资产，不消耗库存。"""
    seen = set()
    asset_ids = []
    for asset_id in body.asset_ids:
        if asset_id in seen:
            continue
        seen.add(asset_id)
        asset_ids.append(asset_id)
    if not asset_ids:
        raise HTTPException(status_code=400, detail="请选择要导出的资产")

    items = []
    with get_db_context() as db:
        for asset_id in asset_ids:
            asset = codex_export_asset(db, asset_id)
            if not asset:
                raise HTTPException(status_code=404, detail=f"资产 {asset_id} 不存在")
            if asset["type"] != "file":
                raise HTTPException(status_code=400, detail=f"{asset['name']} 不是文件资产")
            if asset["category_name"] != "Codex":
                raise HTTPException(status_code=400, detail=f"{asset['name']} 不属于 Codex 分类")

            cpa = redeem_exports._load_cpa_json(asset)
            redeem_exports._validate_codex_payload_format(cpa, body.format, f"资产 {asset_id}", asset)
            items.append((None, asset, cpa))

    headers = {
        "X-Redeemed-Count": str(len(items)),
        "X-Remaining-Count": "0",
        "X-Inventory-Count": "0",
        "X-Reexported": "0",
    }
    if body.format == "text":
        return redeem_exports._export_text(items, headers)
    if body.format == "cpa":
        return redeem_exports._export_cpa(items, headers)
    if body.format == "sub2api_single":
        return redeem_exports._export_sub2api_single(items, headers)
    return redeem_exports._export_auth_json(items, headers)


@router.get("/{asset_id}/file")
def download_asset_file(asset_id: int, _admin: str = Depends(get_current_admin)):
    """管理员受控查看/下载文件资产。"""
    with get_db_context() as db:
        row = db.execute("SELECT id, name, type, file_path FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="资产不存在")
    if row["type"] != "file":
        raise HTTPException(status_code=400, detail="该资产不是文件")

    full_path = resolve_asset_file_path(row["file_path"])
    return FileResponse(full_path, filename=row["name"], media_type="application/octet-stream")


@router.put("/{asset_id}/redeem-status", response_model=AssetResponse)
def update_asset_redeem_status(
    asset_id: int,
    body: AssetRedeemStatusUpdate,
    _admin: str = Depends(get_current_admin),
):
    """管理员手动调整资产是否进入库存池。"""
    with get_db_context(transaction_mode="IMMEDIATE") as db:
        existing = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="资产不存在")

        now = datetime.now(timezone.utc).isoformat()
        affected_cdk_ids = {
            row["cdk_id"]
            for row in db.execute(
                """
                SELECT cdk_id FROM redemption_logs WHERE asset_id = ?
                UNION
                SELECT cdk_id FROM cdk_assets WHERE asset_id = ?
                UNION
                SELECT consumed_by_cdk_id AS cdk_id FROM assets
                WHERE id = ? AND consumed_by_cdk_id IS NOT NULL
                UNION
                SELECT id AS cdk_id FROM cdk_codes WHERE asset_id = ?
                """,
                (asset_id, asset_id, asset_id, asset_id),
            ).fetchall()
            if row["cdk_id"] is not None
        }

        if body.redeemed:
            db.execute("DELETE FROM cdk_assets WHERE asset_id = ? AND consumed_at IS NULL", (asset_id,))
            db.execute(
                """UPDATE assets
                   SET consumed_at = COALESCE(consumed_at, ?),
                       updated_at = ?
                   WHERE id = ?""",
                (now, now, asset_id),
            )
            message = "资产已标记为已兑换"
        else:
            db.execute("DELETE FROM file_download_tokens WHERE asset_id = ?", (asset_id,))
            db.execute("DELETE FROM redemption_logs WHERE asset_id = ?", (asset_id,))
            db.execute("DELETE FROM cdk_assets WHERE asset_id = ?", (asset_id,))
            db.execute(
                "UPDATE assets SET consumed_at = NULL, consumed_by_cdk_id = NULL, updated_at = ? WHERE id = ?",
                (now, asset_id),
            )
            refresh_cdk_usage(db, affected_cdk_ids)
            message = "资产已标记为未兑换"

        row = asset_with_usage(db, asset_id)

    result = row_to_asset(row)
    result["message"] = message
    publish_update(["assets", "cdks", "logs", "dashboard", "inventory"], audience="all")
    return result


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(asset_id: int, _admin: str = Depends(get_current_admin)):
    """获取单个资产详情"""
    with get_db_context() as db:
        row = asset_with_usage(db, asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="资产不存在")
    return row_to_asset(row)


@router.put("/{asset_id}", response_model=AssetResponse)
def update_asset(asset_id: int, body: AssetUpdate, _admin: str = Depends(get_current_admin)):
    """更新资产信息"""
    with get_db_context() as db:
        existing = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="资产不存在")

        updates = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.description is not None:
            updates["description"] = body.description
        if body.content is not None:
            updates["content"] = body.content
        if body.category_id is not None:
            updates["category_id"] = body.category_id

        if updates:
            updates["updated_at"] = datetime.now(timezone.utc).isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [asset_id]
            db.execute(f"UPDATE assets SET {set_clause} WHERE id = ?", values)

        row = db.execute(f"""
            SELECT a.*, c.name as category_name,
                   {redeemed_count_sql("a")} AS redeemed_count
            FROM assets a
            LEFT JOIN categories c ON a.category_id = c.id WHERE a.id = ?
        """, (asset_id,)).fetchone()
    publish_update(["assets", "categories", "dashboard", "inventory"], audience="all")
    return row_to_asset(row)


@router.delete("/{asset_id}")
def delete_asset(asset_id: int, _admin: str = Depends(get_current_admin)):
    """删除资产及其关联的文件和CDK"""
    with get_db_context() as db:
        row = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="资产不存在")

        block_reason = get_asset_delete_block(db, asset_id)
        if block_reason:
            raise HTTPException(status_code=409, detail=block_reason)

        try:
            db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="资产仍有关联数据，不能删除") from exc

        # 删除关联的物理文件
        if row["file_path"]:
            full_path = resolve_asset_file_path(row["file_path"], must_exist=False)
            if os.path.exists(full_path):
                os.remove(full_path)

    publish_update(["assets", "categories", "dashboard", "inventory"], audience="all")
    return {"success": True, "message": "资产已删除"}
