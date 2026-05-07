"""
CDK Vaults — FastAPI 应用入口
启动命令: uv run cdk-vaults --reload --port 8000
"""

import os
from dotenv import load_dotenv

# 加载 .env 配置 (必须在导入其他模块之前)
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from server.database import init_db
from server.routes import admin, assets, categories, cdks, redeem

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期 — 启动时初始化数据库"""
    init_db()
    yield


app = FastAPI(
    title="CDK Vaults",
    description="资产管理 & CDK 兑换系统",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API 路由 ──────────────────────────────────────────
app.include_router(admin.router, prefix="/api/admin", tags=["Admin"])
app.include_router(categories.router, prefix="/api/categories", tags=["Categories"])
app.include_router(assets.router, prefix="/api/assets", tags=["Assets"])
app.include_router(cdks.router, prefix="/api/cdks", tags=["CDKs"])
app.include_router(redeem.router, prefix="/api/redeem", tags=["Redeem"])

# ── 静态文件 ──────────────────────────────────────────
# 上传目录只作为内部存储，不再通过 /uploads 静态暴露。
UPLOAD_DIR = os.path.join(BASE_DIR, "server", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "public"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "admin"), exist_ok=True)


@app.get("/admin", include_in_schema=False)
def admin_index_redirect():
    return RedirectResponse(url="/admin/", status_code=308)


app.mount("/admin", StaticFiles(directory=os.path.join(BASE_DIR, "admin"), html=True), name="admin")
app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "public"), html=True), name="public")
