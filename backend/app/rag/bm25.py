"""BM25 关键词检索与 Rerank。"""
from __future__ import annotations

import threading
from typing import Any

from loguru import logger

from app.core.config import settings
from app.rag.vector_store import SearchHit


class BM25Retriever:
    """基于 rank-bm25 的关键词检索（无外部服务依赖）。"""

    def __init__(self, corpus: list[tuple[str, str, dict[str, Any]]]) -> None:
        """corpus: [(id, text, metadata), ...]"""
        self.items = corpus
        self.texts = [t for _, t, _ in corpus]
        try:
            from rank_bm25 import BM25Okapi

            self._bm25 = BM25Okapi([_tokenize(t) for t in self.texts])
        except Exception:  # noqa: BLE001
            self._bm25 = None

    def search(self, query: str, top_k: int) -> list[SearchHit]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        hits = []
        for i in ranked:
            cid, text, meta = self.items[i]
            # 归一化分数到 0~1
            max_s = max(scores) if scores.size else 1
            norm = float(scores[i] / max_s) if max_s > 0 else 0.0
            hits.append(SearchHit(cid, text, norm, meta))
        return hits


def _tokenize(text: str) -> list[str]:
    import re

    # 中英文简单分词：英文按空格，中文按字符
    tokens = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", text.lower())
    return tokens


# ---------- Rerank 模型缓存 ----------
# CrossEncoder 加载要读几百 MB 权重（数秒级）。原来每次 rerank 都 `CrossEncoder(...)`
# 重建一遍，一次生成流水线会重复加载多次，开销远超推理本身。这里按模型名缓存，
# 并用锁保证并发首次加载只发生一次。缓存的是 (model, load_error)——加载失败也记住，
# 避免每轮都去重试一个注定失败的加载。
_rerank_lock = threading.Lock()
_rerank_models: dict[str, Any] = {}
_rerank_errors: dict[str, str] = {}
# 正在加载中的模型：防止多个线程同时 miss 同一模型时各自重复加载（几字节的
# 竞态窗口会白白加载两份几百 MB 的权重）。加载方完成后负责与条件变量一起唤醒等待者。
_loading: dict[str, threading.Event] = {}


def _get_cross_encoder(model_name: str):
    """按模型名获取（并缓存）CrossEncoder；加载失败返回 None 并缓存失败原因。

    并发安全：同一模型只会有一个线程实际加载，其余线程等待其完成，避免重复读权重。
    """
    while True:
        with _rerank_lock:
            if model_name in _rerank_models:
                return _rerank_models[model_name]
            if model_name in _rerank_errors:
                return None
            event = _loading.get(model_name)
            if event is not None:
                # 有其它线程正在加载：等待其完成，再回到循环顶部读取结果（或失败）
                pass
            else:
                # 我是加载方：登记在途状态后跳出循环去锁外加载
                event = threading.Event()
                _loading[model_name] = event
                break
        # 等待加载方完成（带超时，避免异常导致死等）
        event.wait(timeout=120.0)
        continue

    # 锁外执行耗时加载
    try:
        from sentence_transformers import CrossEncoder

        model = CrossEncoder(model_name)
    except Exception as exc:  # noqa: BLE001  依赖缺失 / 权重不可下载 → 永久降级
        with _rerank_lock:
            _rerank_errors[model_name] = f"{type(exc).__name__}: {exc}"
            _loading.pop(model_name, None).set()
        logger.warning(f"Rerank 模型 {model_name} 加载失败，后续将跳过 rerank: {exc}")
        return None
    with _rerank_lock:
        _rerank_models[model_name] = model
        loading_event = _loading.pop(model_name, None)
    if loading_event is not None:
        loading_event.set()  # 唤醒等待者
    return model


def rerank(query: str, hits: list[SearchHit]) -> list[SearchHit]:
    """可选 Rerank（provider=none 时直接返回）。"""
    if settings.rerank_provider == "none" or not hits:
        return hits
    model = _get_cross_encoder(settings.rerank_model)
    if model is None:
        return hits[: settings.rerank_top_n]
    try:
        pairs = [(query, h.text) for h in hits]
        scores = model.predict(pairs)
        for h, s in zip(hits, scores):
            h.score = float(s)
        hits.sort(key=lambda h: h.score, reverse=True)
    except Exception as exc:  # noqa: BLE001  rerank 失败时保留原排序
        logger.warning(f"Rerank 推理失败，保留原排序: {exc}")
    return hits[: settings.rerank_top_n]
