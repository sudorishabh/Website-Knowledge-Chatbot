from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.search import router as search_router
from app.api.suggestions import router as suggestions_router
from app.app_factory import create_base_app

# Retrieval server: read-only query API (chat / search / the widget's suggested
# questions) + health probes. Ingestion lives in a separate server — see
# app.ingest_main.
app = create_base_app("Agentic RAG Chatbot")

app.include_router(health_router)
app.include_router(chat_router)
app.include_router(search_router)
app.include_router(suggestions_router)
