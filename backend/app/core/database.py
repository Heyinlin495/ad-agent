"""SQLAlchemy 2.0 引擎与会话管理。"""
from collections.abc import Generator

from loguru import logger
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

_IS_SQLITE = settings.database_url.startswith("sqlite")

# SQLite 需要 check_same_thread=False 以支持 FastAPI 多线程；
# timeout 即 sqlite3 的 busy_timeout（秒），锁等待超时才报错，避免并发写立即失败。
connect_args = (
    {"check_same_thread": False, "timeout": 30} if _IS_SQLITE else {}
)

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    echo=False,
)


if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        """每条 SQLite 连接启用 WAL 与 busy_timeout。

        后台任务线程池（task_max_workers 个线程）+ Web 请求线程会并发写同一
        .db 文件，而 SQLite 只允许单写者。默认 journal_mode=delete 且无
        busy_timeout 时，写冲突会立刻抛 "database is locked"（被 run_task 的
        except 吞成"任务失败"，随机且难复现）。WAL 允许读写并发，busy_timeout
        让写冲突排队等待而非直接失败。
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 后加的列：{表名: [(列名, DDL 语句)]}，供 create_all 无法覆盖的存量库做轻量升级。
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "ad_tasks": [
        ("cancel_requested", "ALTER TABLE ad_tasks ADD COLUMN cancel_requested BOOLEAN DEFAULT 0"),
        ("num_versions", "ALTER TABLE ad_tasks ADD COLUMN num_versions INTEGER DEFAULT 3"),
        ("product_override", "ALTER TABLE ad_tasks ADD COLUMN product_override JSON"),
        ("ad_theme", "ALTER TABLE ad_tasks ADD COLUMN ad_theme VARCHAR(255) DEFAULT ''"),
    ],
}


def _ensure_columns() -> None:
    """为已存在的表补齐后加的列（SQLite/Postgres 通用，无 Alembic 时的兜底）。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, columns in _ADDED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns:
            if name in existing:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                logger.info(f"轻量迁移：已为 {table} 补充列 {name}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"轻量迁移失败 {table}.{name}: {exc}")


def init_db() -> None:
    """建立/升级数据库结构。

    默认优先执行 Alembic 迁移（settings.auto_migrate）；对早期由 create_all
    建出的存量库（无 alembic_version）先补齐后加列，再 stamp 到最新版本，
    使其进入 Alembic 纳管，后续迁移可持续演进。任何情况下都保留轻量兜底。
    """
    # 导入模型以注册到 Base.metadata
    from app import models  # noqa: F401

    if settings.auto_migrate:
        from app.core.migrations import has_alembic_version, run_migrations, stamp_head

        if has_alembic_version() and run_migrations():
            return
        Base.metadata.create_all(bind=engine)
        _ensure_columns()
        if not has_alembic_version():
            stamp_head()
        return

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
