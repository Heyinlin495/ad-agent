"""Milvus 向量库实现（生产环境可选，需运行 Milvus 服务）。"""
from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.rag.vector_store import SearchHit, VectorStore


class MilvusVectorStore(VectorStore):
    """基于 pymilvus 的向量库。"""

    COLLECTION = "kb_chunks"

    def __init__(self) -> None:
        from pymilvus import (
            Collection,
            CollectionSchema,
            DataType,
            FieldSchema,
            connections,
            utility,
        )

        self.pymilvus = __import__("pymilvus")
        connections.connect(uri=settings.milvus_uri, timeout=10)
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=128),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=settings.embedding_dim),
            FieldSchema(name="doc_id", dtype=DataType.INT64),
        ]
        if not utility.has_collection(self.COLLECTION):
            schema = CollectionSchema(fields, description="knowledge base chunks")
            self.collection = Collection(self.COLLECTION, schema=schema)
            self.collection.create_index(
                field_name="embedding",
                index_params={"metric_type": "IP", "index_type": "IVF_FLAT", "params": {"nlist": 128}},
            )
        else:
            self.collection = Collection(self.COLLECTION)
        self.collection.load()

    def add(
        self,
        ids: list[str],
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        rows = [
            {
                "id": ids[i],
                "text": texts[i],
                "embedding": embeddings[i],
                "doc_id": int(metadatas[i].get("document_id", 0)),
            }
            for i in range(len(ids))
        ]
        self.collection.insert(rows)
        self.collection.flush()

    def query(
        self, query_embedding: list[float], top_k: int, where: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        expr = None
        if where and "document_id" in where:
            expr = f'doc_id == {where["document_id"]}'
        params = {"metric_type": "IP", "params": {"nprobe": 10}}
        res = self.collection.search(
            data=[query_embedding],
            anns_field="embedding",
            param=params,
            limit=top_k,
            expr=expr,
            output_fields=["id", "text"],
        )
        hits: list[SearchHit] = []
        for hit in res[0]:
            hits.append(
                SearchHit(id=hit.id, text=hit.entity.get("text", ""), score=float(hit.score))
            )
        return hits

    def delete(self, ids: list[str]) -> None:
        if ids:
            self.collection.delete(expr=f"id in {ids}")

    def clear(self) -> None:
        from pymilvus import utility

        utility.drop_collection(self.COLLECTION)

    def count(self) -> int:
        return self.collection.num_entities
