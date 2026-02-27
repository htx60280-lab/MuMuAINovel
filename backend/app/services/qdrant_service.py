"""Qdrant 向量数据库服务封装"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from app.config import settings
from app.logger import get_logger


logger = get_logger(__name__)


@dataclass
class QdrantSearchResult:
    """Qdrant 检索结果"""
    memory_id: str
    score: float
    payload: Dict[str, Any]


class QdrantService:
    """Qdrant 向量服务"""

    def __init__(self, url: Optional[str] = None, api_key: Optional[str] = None) -> None:
        self.url = url or settings.qdrant_url
        self.api_key = api_key or settings.qdrant_api_key
        self._client: Optional[QdrantClient] = None

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            self._client = QdrantClient(url=self.url, api_key=self.api_key)
        return self._client

    def ensure_collection(self, name: str, vector_size: int) -> None:
        """确保集合存在"""
        collections = self.client.get_collections().collections
        if any(col.name == name for col in collections):
            return

        self.client.create_collection(
            collection_name=name,
            vectors_config=qdrant_models.VectorParams(
                size=vector_size,
                distance=qdrant_models.Distance.COSINE
            )
        )
        logger.info(f"✅ Qdrant 创建集合: {name} (dim={vector_size})")

    def collection_exists(self, name: str) -> bool:
        collections = self.client.get_collections().collections
        return any(col.name == name for col in collections)

    def upsert_vectors(
        self,
        collection: str,
        points: List[qdrant_models.PointStruct]
    ) -> None:
        """写入向量"""
        if not points:
            return
        self.client.upsert(collection_name=collection, points=points)

    def delete_by_payload(self, collection: str, key: str, value: str) -> None:
        """按 payload 删除"""
        if not self.collection_exists(collection):
            return
        self.client.delete(
            collection_name=collection,
            points_selector=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key=key,
                        match=qdrant_models.MatchValue(value=value)
                    )
                ]
            )
        )

    def search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int,
        filter_payload: Optional[qdrant_models.Filter] = None
    ) -> List[QdrantSearchResult]:
        """向量检索"""
        results = self.client.search(
            collection_name=collection,
            query_vector=query_vector,
            limit=limit,
            query_filter=filter_payload,
            with_payload=True
        )
        return [
            QdrantSearchResult(
                memory_id=str(hit.id),
                score=float(hit.score),
                payload=hit.payload or {}
            )
            for hit in results
        ]
