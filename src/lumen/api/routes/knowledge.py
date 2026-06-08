from typing import Any

from fastapi import APIRouter, Depends

from lumen.api.deps import get_container
from lumen.models import KnowledgeHit, SearchRequest

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post("/search", response_model=list[KnowledgeHit])
def search_knowledge(payload: SearchRequest, container: Any = Depends(get_container)) -> list[KnowledgeHit]:
    return container.knowledge_store.search(payload.query, payload.limit)
