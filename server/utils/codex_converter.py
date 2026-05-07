"""
CDK Vaults — Codex 格式转换器
CPA JSON → Sub2API 格式
"""

import json
import base64
from datetime import datetime, timezone


def decode_jwt_payload(token: str) -> dict:
    """解码 JWT payload (不验签，仅 base64 解码)"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        # 补齐 base64 padding
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def cpa_to_sub2api_account(cpa: dict) -> dict:
    """将单个 CPA JSON 转为 sub2api account 格式"""
    at_payload = decode_jwt_payload(cpa.get("access_token", ""))
    auth = at_payload.get("https://api.openai.com/auth", {})
    orgs = auth.get("organizations", [])

    email = cpa.get("email", "")
    plan_type = auth.get("chatgpt_plan_type", cpa.get("type", "plus"))
    name_base = email.split("@")[0] if email else "unknown"

    return {
        "name": f"codex-{email}-{plan_type}",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": cpa.get("access_token", ""),
            "chatgpt_account_id": auth.get("chatgpt_account_id", cpa.get("account_id", "")),
            "chatgpt_user_id": auth.get("chatgpt_user_id", ""),
            "client_id": at_payload.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann"),
            "email": email,
            "expires_at": at_payload.get("exp", 0),
            "id_token": cpa.get("id_token", ""),
            "organization_id": orgs[0].get("id", "") if orgs else "",
            "plan_type": plan_type,
            "refresh_token": cpa.get("refresh_token", ""),
        },
        "extra": {"email": email},
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


def wrap_sub2api(accounts: list) -> dict:
    """将 account 列表包装为完整 sub2api 导出格式"""
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "proxies": [],
        "accounts": accounts,
    }
