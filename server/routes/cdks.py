"""
CDK Vaults — CDK 兑换码管理路由
GET    /api/cdks              列出所有 CDK
POST   /api/cdks/generate     批量生成 CDK
PUT    /api/cdks/{id}/status   更新 CDK 状态
DELETE /api/cdks/{id}          删除 CDK
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from server.models import CDKGenerate, CDKResponse, CDKStatusUpdate
from server.auth import get_current_admin
from server.database import get_db_context
from server.utils.cdk_generator import generate_batch

router = APIRouter()


def row_to_cdk(row) -> CDKResponse:
    redemption_count = row["redemption_count"] if "redemption_count" in row.keys() else 0
    return CDKResponse(
        id=row["id"],
        code=row["code"],
        asset_id=row["asset_id"],
        asset_name=row["asset_name"],
        category_id=row["category_id"] if "category_id" in row.keys() else None,
        category_name=row["category_name"] if "category_name" in row.keys() else None,
        status=row["status"],
        max_uses=row["max_uses"],
        used_count=row["used_count"],
        redemption_count=redemption_count,
        can_delete=redemption_count == 0,
        delete_block_reason=(
            f"CDK 已产生 {redemption_count} 条兑换记录，不能删除"
            if redemption_count else None
        ),
        note=row["note"] or "",
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def get_cdk_delete_block(db, cdk_id: int) -> str | None:
    redemption_count = db.execute(
        "SELECT COUNT(*) FROM redemption_logs WHERE cdk_id = ?",
        (cdk_id,),
    ).fetchone()[0]
    if redemption_count:
        return f"CDK 已产生 {redemption_count} 条兑换记录，不能删除"
    return None


@router.get("")
def list_cdks(
    asset_id: int = 0,
    category_id: int = 0,
    status: str = "",
    page: int = 1,
    page_size: int = 20,
    limit: int | None = None,
    paged: bool = False,
    _admin: str = Depends(get_current_admin),
):
    """列出 CDK 兑换码，支持按资产和状态过滤"""
    page = max(page, 1)
    effective_limit = limit if limit is not None else (page_size if paged else 100)
    effective_limit = min(max(effective_limit, 1), 500)
    offset = (page - 1) * effective_limit
    with get_db_context() as db:
        base = """
            FROM cdk_codes c
            LEFT JOIN assets a ON c.asset_id = a.id
            LEFT JOIN categories cat ON cat.id = COALESCE(c.category_id, a.category_id)
            WHERE 1=1
        """
        params = []
        if asset_id:
            base += """
                AND (
                    c.asset_id = ?
                    OR EXISTS (
                        SELECT 1 FROM cdk_assets ca
                        WHERE ca.cdk_id = c.id AND ca.asset_id = ?
                    )
                )
            """
            params.extend([asset_id, asset_id])
        if category_id:
            base += " AND COALESCE(c.category_id, a.category_id) = ?"
            params.append(category_id)
        if status:
            base += " AND c.status = ?"
            params.append(status)
        total = db.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        query = f"""
            SELECT c.*, a.name as asset_name, cat.name as category_name,
                   (SELECT COUNT(*) FROM redemption_logs rl WHERE rl.cdk_id = c.id) AS redemption_count
            {base}
        """
        query += " ORDER BY c.created_at DESC LIMIT ? OFFSET ?"
        rows = db.execute(query, params + [effective_limit, offset]).fetchall()

    items = [row_to_cdk(r) for r in rows]
    if not paged:
        return items
    pages = (total + effective_limit - 1) // effective_limit if total else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": effective_limit,
        "pages": pages,
    }


@router.post("/generate", response_model=list[CDKResponse])
def generate_cdks(body: CDKGenerate, _admin: str = Depends(get_current_admin)):
    """批量生成 CDK 兑换码"""
    with get_db_context() as db:
        category_id = body.category_id
        category_name = "未分类"

        # 兼容旧版 asset_id 调用：用该资产所在分类作为资产池。
        if category_id is None and body.asset_id is not None:
            asset = db.execute("SELECT id, category_id FROM assets WHERE id = ?", (body.asset_id,)).fetchone()
            if not asset:
                raise HTTPException(status_code=404, detail="指定的资产不存在")
            category_id = asset["category_id"]

        if category_id is not None:
            category = db.execute("SELECT id, name FROM categories WHERE id = ?", (category_id,)).fetchone()
            if not category:
                raise HTTPException(status_code=404, detail="指定的分类不存在")
            category_name = category["name"]

        asset_quota = body.max_uses
        total_needed = body.count * asset_quota

        if category_id is None:
            category_clause = "a.category_id IS NULL"
            category_params = []
        else:
            category_clause = "a.category_id = ?"
            category_params = [category_id]

        available_assets = db.execute(
            f"""
            SELECT a.id, a.name
            FROM assets a
            WHERE {category_clause}
              AND a.consumed_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM cdk_assets ca
                  WHERE ca.asset_id = a.id AND ca.consumed_at IS NULL
              )
            ORDER BY a.created_at ASC, a.id ASC
            LIMIT ?
            """,
            [*category_params, total_needed],
        ).fetchall()
        # 获取已存在的码用于排重
        existing = set(
            r[0] for r in db.execute("SELECT code FROM cdk_codes").fetchall()
        )

        # 生成不重复的码
        codes = generate_batch(body.count, prefix=body.prefix, existing_codes=existing)
        if len(codes) < body.count:
            raise HTTPException(status_code=500, detail="无法生成足够的唯一兑换码，请重试")

        # 批量插入
        results = []
        asset_cursor = 0
        for index, code in enumerate(codes):
            assigned = available_assets[asset_cursor:asset_cursor + asset_quota]
            asset_cursor += len(assigned)
            primary_asset = assigned[0] if assigned else None
            cursor = db.execute(
                """INSERT INTO cdk_codes (code, asset_id, category_id, max_uses, note, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    code,
                    primary_asset["id"] if primary_asset else None,
                    category_id,
                    asset_quota,
                    body.note,
                    body.expires_at,
                ),
            )
            cdk_id = cursor.lastrowid
            if assigned:
                db.executemany(
                    "INSERT INTO cdk_assets (cdk_id, asset_id) VALUES (?, ?)",
                    [(cdk_id, item["id"]) for item in assigned],
                )
            results.append(
                CDKResponse(
                    id=cdk_id,
                    code=code,
                    asset_id=primary_asset["id"] if primary_asset else None,
                    asset_name=primary_asset["name"] if primary_asset else None,
                    category_id=category_id,
                    category_name=category_name,
                    status="active",
                    max_uses=asset_quota,
                    used_count=0,
                    redemption_count=0,
                    can_delete=True,
                    delete_block_reason=None,
                    note=body.note,
                    expires_at=body.expires_at,
                    created_at="",
                )
            )

    return results


@router.put("/{cdk_id}/status")
def update_cdk_status(
    cdk_id: int,
    body: CDKStatusUpdate,
    _admin: str = Depends(get_current_admin),
):
    """更新 CDK 状态 (启用/禁用)"""
    with get_db_context() as db:
        row = db.execute("SELECT * FROM cdk_codes WHERE id = ?", (cdk_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="CDK 不存在")
        db.execute("UPDATE cdk_codes SET status = ? WHERE id = ?", (body.status, cdk_id))
    return {"success": True, "message": f"CDK 状态已更新为 {body.status}"}


@router.delete("/{cdk_id}")
def delete_cdk(cdk_id: int, _admin: str = Depends(get_current_admin)):
    """删除单个 CDK"""
    with get_db_context() as db:
        row = db.execute("SELECT * FROM cdk_codes WHERE id = ?", (cdk_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="CDK 不存在")
        block_reason = get_cdk_delete_block(db, cdk_id)
        if block_reason:
            raise HTTPException(status_code=409, detail=block_reason)
        db.execute("DELETE FROM cdk_codes WHERE id = ?", (cdk_id,))
    return {"success": True, "message": "CDK 已删除"}


@router.post("/delete-batch")
def delete_cdks_batch(
    body: dict,
    _admin: str = Depends(get_current_admin),
):
    """按 ID 批量删除 CDK"""
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="请提供要删除的 CDK ID 列表")

    deleted = 0
    blocked = []
    with get_db_context() as db:
        for cdk_id in ids:
            row = db.execute("SELECT id, code FROM cdk_codes WHERE id = ?", (cdk_id,)).fetchone()
            if not row:
                continue
            block_reason = get_cdk_delete_block(db, cdk_id)
            if block_reason:
                blocked.append({"id": cdk_id, "code": row["code"], "reason": block_reason})
                continue
            db.execute("DELETE FROM cdk_codes WHERE id = ?", (cdk_id,))
            deleted += 1

    return {"success": True, "deleted": deleted, "blocked": blocked}


@router.delete("")
def batch_delete_cdks_by_filter(
    asset_id: int = Query(0, description="按资产ID批量删除"),
    status: str = Query("", description="按状态批量删除"),
    _admin: str = Depends(get_current_admin),
):
    """批量删除 CDK"""
    if not asset_id and not status:
        raise HTTPException(status_code=400, detail="请指定 asset_id 或 status 过滤条件")

    with get_db_context() as db:
        query = "DELETE FROM cdk_codes WHERE 1=1"
        params = []
        if asset_id:
            query += " AND asset_id = ?"
            params.append(asset_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        cursor = db.execute(query, params)
        deleted = cursor.rowcount

    return {"success": True, "message": f"已删除 {deleted} 个 CDK"}
