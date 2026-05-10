"""
CDK Vaults — JWT 认证
管理后台的简单密码 + JWT 认证
"""

import os
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

# ── 配置 (从 .env 读取) ──────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "cdk-vaults-dev-secret-change-in-production")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = int(os.environ.get("TOKEN_EXPIRE_HOURS", "24"))

security = HTTPBearer()


def verify_password(password: str) -> bool:
    """验证管理员密码"""
    return hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))


def create_token(expires_delta: Optional[timedelta] = None) -> str:
    """创建 JWT Token"""
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=TOKEN_EXPIRE_HOURS))
    payload = {
        "sub": "admin",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def verify_admin_token(token: str) -> str:
    """验证 JWT Token 字符串，返回管理员标识"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub: str = payload.get("sub")
        if sub != "admin":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的认证凭据",
            )
        return sub
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 已过期或无效",
        )


def get_current_admin(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """验证 JWT Token，返回管理员标识"""
    return verify_admin_token(credentials.credentials)
