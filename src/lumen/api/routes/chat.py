from typing import Any

from fastapi import APIRouter, Depends

from lumen.api.deps import get_container
from lumen.models import AgentRequest, AgentResponse, ConfirmActionRequest

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/ask", response_model=AgentResponse)
async def ask(payload: AgentRequest, container: Any = Depends(get_container)) -> AgentResponse:
    return await container.agent_service.ask(payload)


@router.post("/confirm-action", response_model=AgentResponse)
async def confirm_action(
    payload: ConfirmActionRequest,
    container: Any = Depends(get_container),
) -> AgentResponse:
    return await container.agent_service.confirm_action(payload)
