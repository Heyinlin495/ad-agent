"""数据库迁移：优先使用 Alembic，失败时回退到轻量建表/补列兜底。

- 新库 / 已纳管库（存在 alembic_version 表）：走 `alembic upgrade head`。
- 存量库（由早期 create_all 建出、无 alembic_version）：先建表补齐后加列，
  再 stamp 到最新版本，使其进入 Alembic 纳管，后续迁移可正常演进。
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

from app.core.config import settings

_BACKEND_DIR = Path(__file__).resolve().parents[2]  # backend/


def _alembic_config():  # type: ignore[no-untyped-def]
    """构造 Alembic Config，指向项目内 alembic.ini / 迁移脚本目录。"""
    from alembic.config import Config

    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def has_alembic_version() -> bool:
    """判断数据库是否已被 Alembic 纳管。"""
    from sqlalchemy import inspect

    from app.core.database import engine

    try:
        return "alembic_version" in set(inspect(engine).get_table_names())
    except Exception:  # noqa: BLE001  数据库不可用时交由上层兜底
        return False


def run_migrations() -> bool:
    """执行 `alembic upgrade head`；成功返回 True，失败返回 False（不抛出）。"""
    try:
        from alembic import command

        command.upgrade(_alembic_config(), "head")
        logger.info("Alembic 迁移：数据库已升级到最新版本")
        return True
    except Exception as exc:  # noqa: BLE001  迁移失败不应阻断启动
        logger.warning(f"Alembic 迁移失败，回退轻量建表/补列：{exc}")
        return False


def stamp_head() -> bool:
    """把存量库标记为最新版本（用于从 create_all 平滑迁入 Alembic 纳管）。"""
    try:
        from alembic import command

        command.stamp(_alembic_config(), "head")
        logger.info("Alembic 迁移：已将存量数据库标记为最新版本（stamp head）")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Alembic stamp 失败（不影响运行）：{exc}")
        return False
