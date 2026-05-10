"""
CDK Vaults — 管理员认证路由
POST /api/admin/login     登录获取 Token
GET  /api/admin/stats      获取统计数据
GET  /api/admin/logs       获取兑换记录
GET  /api/admin/upload-logs 获取上传记录
"""

from fastapi import APIRouter, Depends
from server.models import AdminLogin, TokenResponse, StatsResponse, RedemptionLogResponse, RedeemNoticeSettings, AdminPasswordUpdate
from server.auth import verify_password, create_token, get_current_admin, set_admin_password
from server.database import get_db_context, get_redeem_notice, set_redeem_notice
from server.event_bus import publish_update
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


@router.put("/password")
def update_admin_password(body: AdminPasswordUpdate, _admin: str = Depends(get_current_admin)):
    """修改管理员密码。"""
    if not verify_password(body.current_password):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail="新密码不能和当前密码相同")
    set_admin_password(body.new_password)
    return {"success": True, "message": "管理员密码已修改"}


@router.get("/notice")
def get_notice_settings(_admin: str = Depends(get_current_admin)):
    """获取兑换页通知配置。"""
    with get_db_context() as db:
        return get_redeem_notice(db)


@router.put("/notice")
def update_notice_settings(body: RedeemNoticeSettings, _admin: str = Depends(get_current_admin)):
    """更新兑换页通知配置。"""
    content = body.content.strip()
    with get_db_context() as db:
        notice = set_redeem_notice(db, body.enabled, content)
    publish_update(["notice", "dashboard"], audience="all")
    return notice


@router.get("/stats", response_model=StatsResponse)
def get_stats(_admin: str = Depends(get_current_admin)):
    """获取系统统计数据"""
    with get_db_context() as db:
        total_assets = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        unredeemed_assets = db.execute("""
            SELECT COUNT(*)
            FROM assets a
            WHERE a.consumed_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM redemption_logs rl WHERE rl.asset_id = a.id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM cdk_assets ca
                  WHERE ca.asset_id = a.id AND ca.consumed_at IS NOT NULL
              )
        """).fetchone()[0]
        redeemed_assets = max(total_assets - unredeemed_assets, 0)
        total_cdks = db.execute("SELECT COUNT(*) FROM cdk_codes").fetchone()[0]
        active_cdks = db.execute("SELECT COUNT(*) FROM cdk_codes WHERE status='active'").fetchone()[0]
        used_cdks = db.execute("SELECT COUNT(*) FROM cdk_codes WHERE status='used'").fetchone()[0]
        cdk_remaining_quota = db.execute("""
            SELECT COALESCE(SUM(
                CASE
                    WHEN quota_total > used_total THEN quota_total - used_total
                    ELSE 0
                END
            ), 0)
            FROM (
                SELECT
                    MAX(
                        COALESCE(c.max_uses, 1),
                        MAX(
                            COALESCE(c.used_count, 0),
                            (SELECT COUNT(*) FROM cdk_assets ca WHERE ca.cdk_id = c.id AND ca.consumed_at IS NOT NULL),
                            (SELECT COUNT(*) FROM redemption_logs rl WHERE rl.cdk_id = c.id)
                        )
                    ) AS quota_total,
                    MAX(
                        COALESCE(c.used_count, 0),
                        (SELECT COUNT(*) FROM cdk_assets ca WHERE ca.cdk_id = c.id AND ca.consumed_at IS NOT NULL),
                        (SELECT COUNT(*) FROM redemption_logs rl WHERE rl.cdk_id = c.id)
                    ) AS used_total
                FROM cdk_codes c
                WHERE c.status = 'active'
            )
        """).fetchone()[0]
        asset_gap = max(int(cdk_remaining_quota or 0) - int(unredeemed_assets or 0), 0)
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
        unredeemed_assets=unredeemed_assets,
        redeemed_assets=redeemed_assets,
        total_cdks=total_cdks,
        active_cdks=active_cdks,
        used_cdks=used_cdks,
        cdk_remaining_quota=cdk_remaining_quota,
        asset_gap=asset_gap,
        total_redemptions=total_redemptions,
        recent_redemptions=recent_list,
    )


@router.get("/logs")
def get_logs(
    page: int = 1,
    limit: int = 50,
    paged: bool = False,
    search: str = "",
    _admin: str = Depends(get_current_admin),
):
    """获取兑换记录列表"""
    page = max(page, 1)
    limit = min(max(limit, 1), 500)
    offset = (page - 1) * limit
    with get_db_context() as db:
        base = """
            FROM redemption_logs rl
            JOIN cdk_codes c ON rl.cdk_id = c.id
            JOIN assets a ON rl.asset_id = a.id
            LEFT JOIN categories cat ON a.category_id = cat.id
            WHERE 1=1
        """
        params = []
        search = search.strip()
        if search:
            like = f"%{search}%"
            base += """
                AND (
                    c.code LIKE ?
                    OR COALESCE(cat.name, '') LIKE ?
                    OR a.name LIKE ?
                    OR COALESCE(rl.ip_address, '') LIKE ?
                    OR COALESCE(rl.user_agent, '') LIKE ?
                )
            """
            params.extend([like, like, like, like, like])

        total = db.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows = db.execute(f"""
            SELECT rl.id, c.code as cdk_code, cat.name as category_name,
                   a.id as asset_id, a.name as asset_name, a.type as asset_type,
                   rl.ip_address, rl.user_agent, rl.redeemed_at
            {base}
            ORDER BY rl.redeemed_at DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
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


@router.get("/upload-logs")
def get_upload_logs(
    page: int = 1,
    limit: int = 50,
    paged: bool = False,
    search: str = "",
    _admin: str = Depends(get_current_admin),
):
    """获取资产上传/新增记录列表"""
    page = max(page, 1)
    limit = min(max(limit, 1), 500)
    offset = (page - 1) * limit
    with get_db_context() as db:
        base = """
            FROM asset_upload_logs ul
            LEFT JOIN assets a ON a.id = ul.asset_id
            LEFT JOIN categories cat ON cat.id = COALESCE(ul.category_id, a.category_id)
            WHERE 1=1
        """
        params = []
        search = search.strip()
        if search:
            like = f"%{search}%"
            base += """
                AND (
                    COALESCE(NULLIF(ul.asset_name, ''), a.name, '') LIKE ?
                    OR COALESCE(NULLIF(ul.asset_type, ''), a.type, '') LIKE ?
                    OR COALESCE(cat.name, '') LIKE ?
                    OR COALESCE(ul.source, '') LIKE ?
                    OR CASE ul.source
                        WHEN 'manual_create' THEN '手动创建'
                        WHEN 'single_upload' THEN '单文件上传'
                        WHEN 'batch_upload' THEN '批量上传'
                        WHEN 'password_upload' THEN '密码上传'
                        ELSE COALESCE(ul.source, '')
                    END LIKE ?
                    OR COALESCE(ul.original_filename, '') LIKE ?
                    OR COALESCE(ul.status, '') LIKE ?
                    OR CASE ul.status
                        WHEN 'created' THEN '已新增'
                        WHEN 'skipped' THEN '已跳过'
                        WHEN 'failed' THEN '失败'
                        ELSE COALESCE(ul.status, '')
                    END LIKE ?
                    OR COALESCE(ul.message, '') LIKE ?
                )
            """
            params.extend([like, like, like, like, like, like, like, like, like])

        total = db.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows = db.execute(f"""
            SELECT
                ul.id,
                ul.asset_id,
                COALESCE(NULLIF(ul.asset_name, ''), a.name, '') AS asset_name,
                COALESCE(NULLIF(ul.asset_type, ''), a.type, '') AS asset_type,
                ul.category_id,
                cat.name AS category_name,
                ul.source,
                ul.original_filename,
                ul.file_size,
                ul.status,
                ul.message,
                ul.created_at
            {base}
            ORDER BY ul.created_at DESC, ul.id DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
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
