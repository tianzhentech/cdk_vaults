"""
CDK Vaults — 管理员认证路由
POST /api/admin/login     登录获取 Token
GET  /api/admin/stats      获取统计数据
GET  /api/admin/logs       获取兑换记录
"""

from fastapi import APIRouter, Depends
from server.models import AdminLogin, TokenResponse, StatsResponse, RedemptionLogResponse
from server.auth import verify_password, create_token, get_current_admin
from server.database import get_db_context
from fastapi import HTTPException

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def admin_login(body: AdminLogin):
    """管理员登录"""
    if not verify_password(body.password):
        raise HTTPException(status_code=401, detail="密码错误")
    token = create_token()
    return TokenResponse(access_token=token)


@router.get("/verify")
def verify_token(_admin: str = Depends(get_current_admin)):
    """验证 Token 是否有效"""
    return {"valid": True}


@router.get("/stats", response_model=StatsResponse)
def get_stats(_admin: str = Depends(get_current_admin)):
    """获取系统统计数据"""
    with get_db_context() as db:
        total_assets = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        total_cdks = db.execute("SELECT COUNT(*) FROM cdk_codes").fetchone()[0]
        active_cdks = db.execute("SELECT COUNT(*) FROM cdk_codes WHERE status='active'").fetchone()[0]
        used_cdks = db.execute("SELECT COUNT(*) FROM cdk_codes WHERE status='used'").fetchone()[0]
        total_redemptions = db.execute("SELECT COUNT(*) FROM redemption_logs").fetchone()[0]

        recent = db.execute("""
            SELECT rl.id, c.code as cdk_code, cat.name as category_name, a.name as asset_name,
                   rl.ip_address, rl.redeemed_at
            FROM redemption_logs rl
            JOIN cdk_codes c ON rl.cdk_id = c.id
            JOIN assets a ON rl.asset_id = a.id
            LEFT JOIN categories cat ON a.category_id = cat.id
            ORDER BY rl.redeemed_at DESC
            LIMIT 10
        """).fetchall()

        recent_list = [dict(r) for r in recent]

    return StatsResponse(
        total_assets=total_assets,
        total_cdks=total_cdks,
        active_cdks=active_cdks,
        used_cdks=used_cdks,
        total_redemptions=total_redemptions,
        recent_redemptions=recent_list,
    )


@router.get("/logs")
def get_logs(
    page: int = 1,
    limit: int = 50,
    paged: bool = False,
    _admin: str = Depends(get_current_admin),
):
    """获取兑换记录列表"""
    page = max(page, 1)
    limit = min(max(limit, 1), 500)
    offset = (page - 1) * limit
    with get_db_context() as db:
        total = db.execute("SELECT COUNT(*) FROM redemption_logs").fetchone()[0]
        rows = db.execute("""
            SELECT rl.id, c.code as cdk_code, cat.name as category_name, a.name as asset_name,
                   rl.ip_address, rl.user_agent, rl.redeemed_at
            FROM redemption_logs rl
            JOIN cdk_codes c ON rl.cdk_id = c.id
            JOIN assets a ON rl.asset_id = a.id
            LEFT JOIN categories cat ON a.category_id = cat.id
            ORDER BY rl.redeemed_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    items = [dict(r) for r in rows]
    if not paged:
        return items
    pages = (total + limit - 1) // limit if total else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": limit,
        "pages": pages,
    }
