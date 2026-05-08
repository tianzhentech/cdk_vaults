"""
CDK Vaults — 资产管理路由
GET    /api/assets          列出所有资产
POST   /api/assets          创建资产 (文本/链接)
POST   /api/assets/upload   上传文件资产
GET    /api/assets/{id}     获取资产详情
PUT    /api/assets/{id}     更新资产
DELETE /api/assets/{id}     删除资产
"""

import os
import uuid
import shutil
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from server.models import AssetCreate, AssetUpdate, AssetResponse
from server.auth import get_current_admin
from server.database import get_db_context
from server.utils.cdk_allocator import assign_asset_to_pending_cdk

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")


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
    try:
        d["cdk_binding_count"] = row["cdk_binding_count"]
    except (IndexError, KeyError):
        d["cdk_binding_count"] = 0
    d["can_delete"] = d["redeemed_count"] == 0 and d["cdk_binding_count"] == 0
    if d["redeemed_count"] > 0:
        d["delete_block_reason"] = f"资产已产生 {d['redeemed_count']} 条兑换记录，不能删除"
    elif d["cdk_binding_count"] > 0:
        d["delete_block_reason"] = f"资产已绑定 {d['cdk_binding_count']} 个 CDK，不能删除"
    else:
        d["delete_block_reason"] = None
    return d


def get_asset_delete_block(db, asset_id: int) -> str | None:
    redeemed_count = db.execute(
        "SELECT COUNT(*) FROM redemption_logs WHERE asset_id = ?",
        (asset_id,),
    ).fetchone()[0]
    if redeemed_count:
        return f"资产已产生 {redeemed_count} 条兑换记录，不能删除"

    binding_count = db.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT cdk_id FROM cdk_assets WHERE asset_id = ?
            UNION
            SELECT id FROM cdk_codes WHERE asset_id = ?
        )
        """,
        (asset_id, asset_id),
    ).fetchone()[0]
    if binding_count:
        return f"资产已绑定 {binding_count} 个 CDK，不能删除"

    return None


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
               (SELECT COUNT(*) FROM redemption_logs rl WHERE rl.asset_id = a.id) AS redeemed_count,
               (SELECT COUNT(*) FROM (
                   SELECT ca.cdk_id FROM cdk_assets ca WHERE ca.asset_id = a.id
                   UNION
                   SELECT cc.id FROM cdk_codes cc WHERE cc.asset_id = a.id
               )) AS cdk_binding_count
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
                   (SELECT COUNT(*) FROM redemption_logs rl WHERE rl.asset_id = a.id) AS redeemed_count,
                   (SELECT COUNT(*) FROM (
                       SELECT ca.cdk_id FROM cdk_assets ca WHERE ca.asset_id = a.id
                       UNION
                       SELECT cc.id FROM cdk_codes cc WHERE cc.asset_id = a.id
                   )) AS cdk_binding_count
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
            return asset_write_result(skipped_items=[row_to_asset(duplicate)])

        now = datetime.now(timezone.utc).isoformat()
        cursor = db.execute(
            """INSERT INTO assets (name, type, description, content, category_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (asset_name, body.type, body.description, body.content, cat_id, now, now),
        )
        asset_id = cursor.lastrowid
        assign_asset_to_pending_cdk(db, asset_id, cat_id)
        row = db.execute("""
            SELECT a.*, c.name as category_name FROM assets a
            LEFT JOIN categories c ON a.category_id = c.id WHERE a.id = ?
        """, (asset_id,)).fetchone()
    return asset_write_result(created_items=[row_to_asset(row)])


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
    with get_db_context() as db:
        duplicate = find_duplicate_asset(db, asset_name, cat_id, "file")
        if duplicate:
            return asset_write_result(skipped_items=[row_to_asset(duplicate)])

    # 生成唯一文件名，保留原始扩展名
    ext = os.path.splitext(file.filename)[1] if file.filename else ""
    unique_name = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)

    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # 保存文件
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # 存储相对路径
    relative_path = f"/uploads/{unique_name}"

    with get_db_context() as db:
        now = datetime.now(timezone.utc).isoformat()
        cursor = db.execute(
            """INSERT INTO assets (name, type, description, file_path, category_id, created_at, updated_at)
               VALUES (?, 'file', ?, ?, ?, ?, ?)""",
            (asset_name, description, relative_path, cat_id, now, now),
        )
        asset_id = cursor.lastrowid
        assign_asset_to_pending_cdk(db, asset_id, cat_id)
        row = db.execute("""
            SELECT a.*, c.name as category_name FROM assets a
            LEFT JOIN categories c ON a.category_id = c.id WHERE a.id = ?
        """, (asset_id,)).fetchone()
    return asset_write_result(created_items=[row_to_asset(row)])


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

            duplicate = find_duplicate_asset(db, asset_name, cat_id)
            if duplicate:
                skipped.append(row_to_asset(duplicate))
                continue

            ext = os.path.splitext(file.filename)[1] if file.filename else ""
            unique_name = f"{uuid.uuid4().hex}{ext}"
            full_path = os.path.join(UPLOAD_DIR, unique_name)

            with open(full_path, "wb") as f:
                f.write(raw)

            relative_path = f"/uploads/{unique_name}"
            now = datetime.now(timezone.utc).isoformat()
            cursor = db.execute(
                """INSERT INTO assets (name, type, description, file_path, category_id, created_at, updated_at)
                   VALUES (?, 'file', ?, ?, ?, ?, ?)""",
                (asset_name, description, relative_path, cat_id, now, now),
            )
            assign_asset_to_pending_cdk(db, cursor.lastrowid, cat_id)
            row = db.execute("""
                SELECT a.*, c.name as category_name FROM assets a
                LEFT JOIN categories c ON a.category_id = c.id WHERE a.id = ?
            """, (cursor.lastrowid,)).fetchone()
            results.append(row_to_asset(row))

    return asset_write_result(created_items=results, skipped_items=skipped)


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


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(asset_id: int, _admin: str = Depends(get_current_admin)):
    """获取单个资产详情"""
    with get_db_context() as db:
        row = db.execute("""
            SELECT a.*, c.name as category_name FROM assets a
            LEFT JOIN categories c ON a.category_id = c.id WHERE a.id = ?
        """, (asset_id,)).fetchone()
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
            if body.category_id is not None:
                assign_asset_to_pending_cdk(db, asset_id, body.category_id)

        row = db.execute("""
            SELECT a.*, c.name as category_name FROM assets a
            LEFT JOIN categories c ON a.category_id = c.id WHERE a.id = ?
        """, (asset_id,)).fetchone()
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

    return {"success": True, "message": "资产已删除"}
