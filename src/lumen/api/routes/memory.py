from typing import Any

from fastapi import APIRouter, Depends

from lumen.api.deps import get_container
from lumen.models import MemoryHit, SearchRequest

router = APIRouter(prefix="/memory", tags=["memory"])


@router.post("/search", response_model=list[MemoryHit])
def search_memory(payload: SearchRequest, container: Any = Depends(get_container)) -> list[MemoryHit]:
    return container.memory_store.search(payload.query, payload.limit)
