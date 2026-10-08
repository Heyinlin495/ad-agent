"""知识库管理接口。"""
from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.rate_limit import heavy_rate_limit
from app.core.uploads import read_upload_limited
from app.schemas.common import ok
from app.schemas.kb import SearchRequest
from app.services import kb_service

router = APIRouter(prefix="/kb", tags=["knowledge-base"])


@router.post("/documents", dependencies=[Depends(heavy_rate_limit)])
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    country: str = Form(default=""),
    platform: str = Form(default=""),
    category: str = Form(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """上传文档并入库。"""
    data = await read_upload_limited(file)
    doc = kb_service.ingest_document(
        db,
        data,
        file.filename or "doc.txt",
        title or None,
        country or None,
        platform or None,
        category or None,
    )
    return ok(
        {
            "id": doc.id,
            "title": doc.title,
            "status": doc.status,
            "chunk_count": doc.chunk_count,
        }
    )


@router.get("/documents")
def list_documents(db: Session = Depends(get_db)) -> dict:
    docs = kb_service.list_documents(db)
    return ok(
        [
            {
                "id": d.id,
                "title": d.title,
                "source_type": d.source_type,
                "country": d.country,
                "platform": d.platform,
                "category": d.category,
                "status": d.status,
                "chunk_count": d.chunk_count,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in docs
        ]
    )


@router.delete("/documents/{document_id}")
def delete_document(document_id: int, db: Session = Depends(get_db)) -> dict:
    kb_service.delete_document(db, document_id)
    return ok({"deleted": document_id})


@router.post("/search")
def search(req: SearchRequest, db: Session = Depends(get_db)) -> dict:
    result = kb_service.search(
        db,
        req.query,
        top_k=req.top_k,
        country=req.country,
        platform=req.platform,
        category=req.category,
    )
    return ok(result.model_dump())


@router.post("/rebuild", dependencies=[Depends(heavy_rate_limit)])
def rebuild_index(db: Session = Depends(get_db)) -> dict:
    """从数据库块重建向量索引。"""
    count = kb_service.rebuild_index(db)
    return ok({"reindexed_chunks": count})
