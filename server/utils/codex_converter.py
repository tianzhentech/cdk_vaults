"""
CDK Vaults — Codex 格式转换器
CPA JSON → Sub2API 格式
"""

import json
import base64
import re
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


def cpa_access_token(cpa: dict) -> str:
    return str(_first_nested_value(cpa, ("access_token", "accessToken", "access token", "at")) or "").strip()


def cpa_text_fields(cpa: dict) -> tuple[str, str, str]:
    email = _first_nested_value(cpa, ("email", "mail", "邮箱"))
    gpt_password = _first_nested_value(
        cpa,
        (
            "gpt_password",
            "chatgpt_password",
            "openai_password",
            "account_password",
            "account_pass",
            "password",
            "passwd",
            "pass",
            "gpt密码",
            "GPT密码",
            "密码",
        ),
    )
    mail_password = _first_nested_value(
        cpa,
        (
            "email_password",
            "email_pass",
            "email_pwd",
            "mail_password",
            "mail_pass",
            "mail_pwd",
            "imap_password",
            "smtp_password",
            "邮箱密码",
            "邮件密码",
        ),
    )
    return str(email or ""), str(gpt_password or ""), str(mail_password or "")


def cpa_has_text_passwords(cpa: dict) -> bool:
    _email, gpt_password, mail_password = cpa_text_fields(cpa)
    return bool(str(gpt_password).strip() and str(mail_password).strip())


def cpa_to_sub2api_account(cpa: dict) -> dict:
    """将单个 CPA JSON 转为 sub2api account 格式"""
    access_token = cpa_access_token(cpa)
    at_payload = decode_jwt_payload(access_token)
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
            "access_token": access_token,
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


def cpa_to_auth_json(cpa: dict) -> dict:
    """将 CPA JSON 转为 Codex auth.json 格式。"""
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": str(cpa.get("id_token", "")),
            "access_token": cpa_access_token(cpa),
            "refresh_token": str(cpa.get("refresh_token", "")),
            "account_id": str(cpa.get("account_id", "")),
        },
        "last_refresh": str(cpa.get("last_refresh", "")),
    }


def wrap_sub2api(accounts: list) -> dict:
    """将 account 列表包装为完整 sub2api 导出格式"""
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "proxies": [],
        "accounts": accounts,
    }


def _normalize_key(key) -> str:
    return re.sub(r"[\s_\-]+", "", str(key).strip().lower())


def _first_nested_value(data, keys: tuple[str, ...]):
    normalized_keys = [_normalize_key(key) for key in keys]

    if isinstance(data, dict):
        normalized_map = {_normalize_key(key): value for key, value in data.items()}
        for key in normalized_keys:
            value = normalized_map.get(key)
            if value not in (None, ""):
                return value
        for value in data.values():
            found = _first_nested_value(value, keys)
            if found not in (None, ""):
                return found

    if isinstance(data, list):
        for item in data:
            found = _first_nested_value(item, keys)
            if found not in (None, ""):
                return found

    return ""


def _line_part(value) -> str:
    return str(value or "").strip().replace("\r", " ").replace("\n", " ")


def cpa_to_text_line(cpa: dict) -> str:
    """导出为: 邮箱----GPT密码----邮箱密码"""
    email, gpt_password, mail_password = cpa_text_fields(cpa)
    return "----".join((_line_part(email), _line_part(gpt_password), _line_part(mail_password)))
