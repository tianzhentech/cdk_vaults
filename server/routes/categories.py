"""
CDK Vaults — 分类管理路由
GET    /api/categories          列出所有分类
POST   /api/categories          创建分类
PUT    /api/categories/{id}     更新分类
DELETE /api/categories/{id}     删除分类
"""

from fastapi import APIRouter, Depends, HTTPException
from server.models import CategoryCreate, CategoryUpdate, CategoryResponse
from server.auth import get_current_admin
from server.database import get_db_context

router = APIRouter()


def row_to_category(row, asset_count: int = 0) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "color": row["color"] or "#8b5cf6",
        "sort_order": row["sort_order"] or 0,
        "asset_count": asset_count,
        "created_at": row["created_at"],
    }


@router.get("")
def list_categories(
    paged: bool = False,
    page: int = 1,
    page_size: int = 20,
    _admin: str = Depends(get_current_admin),
):
    """列出所有分类，附带每个分类下的资产数量"""
    with get_db_context() as db:
        if not paged:
            rows = db.execute("""
                SELECT c.*, COUNT(a.id) as asset_count
                FROM categories c
                LEFT JOIN assets a ON a.category_id = c.id
                GROUP BY c.id
                ORDER BY c.sort_order ASC, c.created_at ASC
            """).fetchall()
            return [row_to_category(r, r["asset_count"]) for r in rows]

        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        total = db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        offset = (page - 1) * page_size
        rows = db.execute("""
            SELECT c.*, COUNT(a.id) as asset_count
            FROM categories c
            LEFT JOIN assets a ON a.category_id = c.id
            GROUP BY c.id
            ORDER BY c.sort_order ASC, c.created_at ASC
            LIMIT ? OFFSET ?
        """, (page_size, offset)).fetchall()
    pages = (total + page_size - 1) // page_size if total else 1
    return {
        "items": [row_to_category(r, r["asset_count"]) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post("/delete-batch")
def delete_categories_batch(
    body: dict,
    _admin: str = Depends(get_current_admin),
):
    """批量删除分类"""
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="请提供要删除的分类 ID 列表")

    deleted = 0
    blocked = []
    with get_db_context() as db:
        for cat_id in ids:
            row = db.execute("SELECT id, name FROM categories WHERE id = ?", (cat_id,)).fetchone()
            if not row:
                continue
            if row["name"] == "Codex":
                blocked.append({"id": cat_id, "name": row["name"], "reason": "内置 Codex 分类不可删除"})
                continue
            db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
            deleted += 1

    return {"success": True, "deleted": deleted, "blocked": blocked}


@router.post("", response_model=CategoryResponse)
def create_category(body: CategoryCreate, _admin: str = Depends(get_current_admin)):
    """创建分类"""
    with get_db_context() as db:
        existing = db.execute("SELECT id FROM categories WHERE name = ?", (body.name,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="分类名称已存在")
        cursor = db.execute(
            "INSERT INTO categories (name, description, color, sort_order) VALUES (?, ?, ?, ?)",
            (body.name, body.description, body.color, body.sort_order),
        )
        row = db.execute("SELECT * FROM categories WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return row_to_category(row)


@router.put("/{cat_id}", response_model=CategoryResponse)
def update_category(cat_id: int, body: CategoryUpdate, _admin: str = Depends(get_current_admin)):
    """更新分类"""
    with get_db_context() as db:
        existing = db.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="分类不存在")

        updates = {}
        if body.name is not None:
            dup = db.execute("SELECT id FROM categories WHERE name = ? AND id != ?", (body.name, cat_id)).fetchone()
            if dup:
                raise HTTPException(status_code=400, detail="分类名称已存在")
            updates["name"] = body.name
        if body.description is not None:
            updates["description"] = body.description
        if body.color is not None:
            updates["color"] = body.color
        if body.sort_order is not None:
            updates["sort_order"] = body.sort_order

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [cat_id]
            db.execute(f"UPDATE categories SET {set_clause} WHERE id = ?", values)

        row = db.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
        count = db.execute("SELECT COUNT(*) FROM assets WHERE category_id = ?", (cat_id,)).fetchone()[0]
    return row_to_category(row, count)


@router.delete("/{cat_id}")
def delete_category(cat_id: int, _admin: str = Depends(get_current_admin)):
    """删除分类 (关联资产的 category_id 会被置为 NULL)"""
    with get_db_context() as db:
        existing = db.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="分类不存在")
        if existing["name"] == "Codex":
            raise HTTPException(status_code=400, detail="内置 Codex 分类不可删除")
        db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    return {"success": True, "message": "分类已删除"}
