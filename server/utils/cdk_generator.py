"""
CDK Vaults — CDK 兑换码生成器
生成格式: PREFIX-XXXX-XXXX-XXXX
排除易混淆字符: 0/O, 1/I/l
"""

import secrets
import string

# 排除容易混淆的字符
CHARSET = "".join(
    c for c in string.ascii_uppercase + string.digits
    if c not in "0O1I"
)  # A-Z (without O,I) + 2-9 = 32 characters


def generate_code(prefix: str = "CDK", segment_length: int = 4, segments: int = 3) -> str:
    """
    生成单个 CDK 兑换码
    默认格式: CDK-XXXX-XXXX-XXXX
    """
    parts = [prefix.upper()]
    for _ in range(segments):
        segment = "".join(secrets.choice(CHARSET) for _ in range(segment_length))
        parts.append(segment)
    return "-".join(parts)


def generate_batch(
    count: int,
    prefix: str = "CDK",
    segment_length: int = 4,
    segments: int = 3,
    existing_codes: set = None,
) -> list[str]:
    """
    批量生成不重复的 CDK 兑换码
    会排除数据库中已存在的码
    """
    if existing_codes is None:
        existing_codes = set()

    codes = []
    attempts = 0
    max_attempts = count * 10  # 防止无限循环

    while len(codes) < count and attempts < max_attempts:
        code = generate_code(prefix, segment_length, segments)
        if code not in existing_codes and code not in codes:
            codes.append(code)
        attempts += 1

    return codes
