"""
CDK Vaults — Pydantic 数据模型
请求/响应的数据验证与序列化
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ──────────────────────── 分类 (Category) ────────────────────────

class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="分类名称")
    description: str = Field(default="", max_length=500, description="分类描述")
    color: str = Field(default="#8b5cf6", max_length=20, description="显示颜色")
    sort_order: int = Field(default=0, description="排序权重")


class CategoryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    color: Optional[str] = Field(default=None, max_length=20)
    sort_order: Optional[int] = None


class CategoryResponse(BaseModel):
    id: int
    name: str
    description: str
    color: str
    sort_order: int
    asset_count: int = 0
    created_at: str


# ──────────────────────── 资产 (Asset) ────────────────────────

class AssetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="资产名称")
    type: str = Field(..., pattern="^(file|text|link)$", description="资产类型")
    description: str = Field(default="", max_length=2000, description="资产描述")
    content: Optional[str] = Field(default=None, description="文本内容或链接URL")
    category_id: Optional[int] = Field(default=None, description="分类ID")


class AssetUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    content: Optional[str] = None
    category_id: Optional[int] = None


class AssetResponse(BaseModel):
    id: int
    name: str
    type: str
    description: str
    file_path: Optional[str] = None
    download_url: Optional[str] = None
    content: Optional[str] = None
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    thumbnail: Optional[str] = None
    redeemed_count: int = 0
    cdk_binding_count: int = 0
    can_delete: bool = True
    delete_block_reason: Optional[str] = None
    created_at: str
    updated_at: str


# ──────────────────────── CDK 兑换码 ────────────────────────

class CDKGenerate(BaseModel):
    asset_id: Optional[int] = Field(default=None, description="兼容旧版：用资产所属分类生成 CDK")
    category_id: Optional[int] = Field(default=None, description="兑换资产分类ID")
    count: int = Field(default=1, ge=1, le=1000, description="生成数量")
    max_uses: int = Field(default=1, ge=1, le=99999, description="每个CDK可兑换的资产数")
    prefix: str = Field(default="CDK", max_length=10, description="CDK前缀")
    note: str = Field(default="", max_length=500, description="批次备注")
    expires_at: Optional[str] = Field(default=None, description="过期时间 (ISO 格式)")


class CDKResponse(BaseModel):
    id: int
    code: str
    asset_id: Optional[int] = None
    asset_name: Optional[str] = None
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    status: str
    max_uses: int
    used_count: int
    redemption_count: int = 0
    can_delete: bool = True
    delete_block_reason: Optional[str] = None
    note: str
    expires_at: Optional[str] = None
    created_at: str


class CDKStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(active|disabled)$", description="新状态")


# ──────────────────────── 兑换 (Redeem) ────────────────────────

class RedeemRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=50, description="CDK兑换码")
    quantity: int = Field(default=1, ge=1, le=1000, description="本次兑换资产数量")


class CodexRedeemRequest(BaseModel):
    codes: list[str] = Field(..., min_length=1, description="CDK兑换码列表")
    format: str = Field(default="cpa", pattern="^(cpa|sub2api_single|sub2api_multi|text)$", description="导出格式")
    quantity: int = Field(default=1, ge=1, le=1000, description="每个CDK本次导出资产数量")


class RedeemResponse(BaseModel):
    success: bool
    message: str
    asset: Optional[AssetResponse] = None
    assets: list[AssetResponse] = []
    redeemed_count: int = 0
    remaining_count: int = 0
    inventory_count: int = 0
    total_count: int = 0


# ──────────────────────── 管理员 (Admin) ────────────────────────

class AdminLogin(BaseModel):
    password: str = Field(..., min_length=1, description="管理员密码")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ──────────────────────── 统计 (Stats) ────────────────────────

class StatsResponse(BaseModel):
    total_assets: int
    total_cdks: int
    active_cdks: int
    used_cdks: int
    total_redemptions: int
    recent_redemptions: list = []


# ──────────────────────── 兑换记录 ────────────────────────

class RedemptionLogResponse(BaseModel):
    id: int
    cdk_code: str
    category_name: Optional[str] = None
    asset_name: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    redeemed_at: str
