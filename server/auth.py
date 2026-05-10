"""
CDK Vaults — JWT 认证
管理后台的简单密码 + JWT 认证
"""

import os
import hmac
import base64
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from server.database import get_db_context, get_setting, set_setting

# ── 配置 (从 .env 读取) ──────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "cdk-vaults-dev-secret-change-in-production")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = int(os.environ.get("TOKEN_EXPIRE_HOURS", "24"))
ADMIN_PASSWORD_HASH_KEY = "admin_password_hash"
PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 210_000

security = HTTPBearer()


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_urlsafe(18)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_HASH_ITERATIONS,
    )
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{PASSWORD_HASH_ALGORITHM}${PASSWORD_HASH_ITERATIONS}${salt}${encoded}"


def _verify_password_hash(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt, expected = password_hash.split("$", 3)
        if algorithm != PASSWORD_HASH_ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        )
        actual = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _get_admin_password_hash() -> str:
    try:
        with get_db_context() as db:
            return get_setting(db, ADMIN_PASSWORD_HASH_KEY, "")
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return ""
        raise


def verify_password(password: str) -> bool:
    """验证管理员密码"""
    password_hash = _get_admin_password_hash()
    if password_hash:
        return _verify_password_hash(password, password_hash)
    return hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))


def set_admin_password(password: str):
    """设置管理员密码。密码只以哈希形式保存。"""
    with get_db_context() as db:
        set_setting(db, ADMIN_PASSWORD_HASH_KEY, _hash_password(password))


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
