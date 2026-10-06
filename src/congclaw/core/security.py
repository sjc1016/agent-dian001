"""口令哈希与校验（仅依赖 Python 标准库）。

演示环境采用 PBKDF2-HMAC-SHA256：盐值随每个账号随机生成并编码进哈希串，
因此相同口令在不同账号下的哈希值互不相同，数据库中不保存明文口令。

哈希串格式：``pbkdf2_sha256$<迭代次数>$<盐>$<摘要>``
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
# 演示机上的取舍：迭代次数兼顾安全与登录延迟（单次约数十毫秒）
ITERATIONS = 120_000
SALT_BYTES = 16

# 演示环境所有存量账号的统一初始口令
DEFAULT_PASSWORD = "123456"

# 登录口令长度下限（注册时校验）
MIN_PASSWORD_LENGTH = 6


def hash_password(password: str) -> str:
    """生成带随机盐的口令哈希串。"""
    salt = secrets.token_hex(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), ITERATIONS
    ).hex()
    return f"{ALGORITHM}${ITERATIONS}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    """校验口令是否与哈希串匹配；哈希串缺失或格式非法一律视为不匹配。"""
    if not password or not encoded:
        return False
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        rounds = int(iterations)
    except (ValueError, AttributeError):
        return False
    if algorithm != ALGORITHM or rounds <= 0:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds
    ).hex()
    # 恒定时间比较，避免逐字符提前返回泄露信息
    return hmac.compare_digest(candidate, digest)
