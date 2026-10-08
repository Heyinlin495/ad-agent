"""pytest 共享配置：Mock 模式 + 临时目录，无外部依赖运行。"""
from __future__ import annotations

import os
import tempfile

# 必须在导入 app 之前设置环境变量
_tmp = tempfile.mkdtemp(prefix="adagent_test_").replace("\\", "/")
os.environ["ENVIRONMENT"] = "test"
os.environ["MOCK_MODE"] = "true"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["VISION_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["VECTOR_STORE_TYPE"] = "mock"
os.environ["IMAGE_PROVIDER"] = "mock"
os.environ["STORAGE_TYPE"] = "local"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["STORAGE_DIR"] = f"{_tmp}/storage"
os.environ["CHROMA_DIR"] = f"{_tmp}/chroma"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def db_session():
    from app.core.database import SessionLocal

    db = SessionLocal()
    yield db
    db.close()
