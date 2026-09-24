"""Infrastructure client gateways.

Lazily-created, ``@lru_cache``-memoized handles to the external services the app
depends on — the embedding model, the chat LLM, the Qdrant vector store, the
MySQL pool and Redis. Feature packages (ingestion, retrieval, generation,
pipeline) depend on this layer; it depends only on ``app.config``.
"""
from __future__ import annotations

from app.core.clients.cache import get_redis
from app.core.clients.database import (
    MySQLPool,
    get_mysql_pool,
    mysql_connection,
    new_mysql_connection,
)
from app.core.clients.graph import (
    get_graph_driver,
    graph_available,
    read_session,
    reset_graph_driver,
    write_session,
)
from app.core.clients.embeddings import (
    embed_query,
    embedding_version,
    get_embeddings,
)
from app.core.clients.llm import get_llm, get_structured_llm
from app.core.clients.vector_store import (
    delete_document,
    ensure_collection,
    get_qdrant_client,
    refresh_document_title,
)

__all__ = [
    "embed_query",
    "embedding_version",
    "get_embeddings",
    "get_llm",
    "get_structured_llm",
    "get_qdrant_client",
    "get_graph_driver",
    "graph_available",
    "read_session",
    "write_session",
    "reset_graph_driver",
    "ensure_collection",
    "delete_document",
    "refresh_document_title",
    "MySQLPool",
    "get_mysql_pool",
    "mysql_connection",
    "new_mysql_connection",
    "get_redis",
]
