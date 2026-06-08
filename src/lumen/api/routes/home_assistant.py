from typing import Any

from fastapi import APIRouter, Depends

from lumen.api.deps import get_container

router = APIRouter(prefix="/home-assistant", tags=["home-assistant"])


@router.get("/entities")
async def list_entities(container: Any = Depends(get_container)) -> dict:
    if not container.home_assistant.available:
        return {"status": "unavailable", "entities": []}
    entities = await container.home_assistant.list_entities()
    return {"status": "ok", "entities": entities}


@router.get("/snapshot")
async def snapshot_entities(container: Any = Depends(get_container)) -> dict:
    if not container.home_assistant.available:
        return {"status": "unavailable", "groups": {}}
    snapshot = await container.home_assistant.snapshot_entities()
    return {
        "status": "ok",
        "groups": {key: len(value) for key, value in snapshot.items()},
        "snapshot": snapshot,
    }
