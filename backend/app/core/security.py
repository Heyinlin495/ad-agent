"""启动期安全自检。

生产环境（ENVIRONMENT=production）且 STRICT_SECURITY=true 时，若发现致命
安全配置问题（CORS 通配、静态资源无鉴权、真实模型缺密钥等）将直接拒绝启动，
避免带着不安全配置上线；非生产环境仅打印告警。
"""
from __future__ import annotations

from loguru import logger

from app.core.config import settings


class SecurityError(RuntimeError):
    """生产环境存在致命安全问题。"""


def run_security_audit() -> list[str]:
    """执行安全自检并返回问题列表。

    生产环境 + strict_security 时抛出 SecurityError 阻止启动。
    """
    issues = settings.security_issues()
    if not issues:
        logger.info("安全自检通过：未发现明显风险配置")
        return issues

    for issue in issues:
        logger.warning(f"[安全自检] {issue}")

    if settings.is_production and settings.strict_security:
        raise SecurityError(
            "生产环境安全自检未通过，已拒绝启动（设置 STRICT_SECURITY=false 可仅告警）：\n- "
            + "\n- ".join(issues)
        )
    return issues
