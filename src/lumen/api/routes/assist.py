import logging
from typing import Any

from fastapi import APIRouter, Depends

from lumen.api.deps import get_container
from lumen.formatting import escape_markdown_v2
from lumen.models import (
    AgentRequest,
    ConfirmActionRequest,
    HomeAssistantAssistRequest,
    HomeAssistantAssistResponse,
)

router = APIRouter(prefix="/assist", tags=["assist"])
logger = logging.getLogger(__name__)


@router.post("/process", response_model=HomeAssistantAssistResponse)
async def process_assist(payload: HomeAssistantAssistRequest, container: Any = Depends(get_container)) -> HomeAssistantAssistResponse:
    logger.info(
        "assist.process received conversation_id=%s user_id=%s text=%r allow_actions=%s exposed_entities=%d",
        payload.conversation_id,
        payload.user_id,
        payload.text,
        payload.allow_actions,
        len(payload.exposed_entities),
    )
    request = AgentRequest(
        text=payload.text,
        source="home_assistant_assist",
        user_id=payload.user_id,
        session_id=payload.session_id,
        conversation_id=payload.conversation_id,
        allow_actions=payload.allow_actions,
        context_overrides={
            "language": payload.language,
            "exposed_entities": payload.exposed_entities,
            "metadata": payload.metadata,
        },
    )
    response = await container.agent_service.ask(request)
    logger.info(
        "assist.process completed conversation_id=%s requires_confirmation=%s speech=%r",
        payload.conversation_id,
        response.requires_confirmation,
        response.answer[:200],
    )
    return _to_assist_response(response, payload.conversation_id)


@router.post("/confirm", response_model=HomeAssistantAssistResponse)
async def confirm_assist_action(payload: ConfirmActionRequest, container: Any = Depends(get_container)) -> HomeAssistantAssistResponse:
    logger.info(
        "assist.confirm received conversation_id=%s user_id=%s action_id=%s confirmed=%s",
        payload.conversation_id,
        payload.user_id,
        payload.action_id,
        payload.confirmed,
    )
    response = await container.agent_service.confirm_action(payload)
    logger.info(
        "assist.confirm completed conversation_id=%s action_id=%s speech=%r",
        payload.conversation_id,
        payload.action_id,
        response.answer[:200],
    )
    return HomeAssistantAssistResponse(
        response_type="action_result",
        speech=escape_markdown_v2(response.answer),
        conversation_id=payload.conversation_id,
        continue_conversation=True,
        requires_confirmation=False,
        data={},
    )


def _to_assist_response(response, conversation_id: str) -> HomeAssistantAssistResponse:
    action_id = response.action_proposal.action_id if response.action_proposal else None
    action_label = response.action_proposal.label if response.action_proposal else None
    data = {
        "citations": [item.model_dump() for item in response.citations],
        "memory_hits": [item.model_dump() for item in response.memory_hits],
        "knowledge_hits": [item.model_dump() for item in response.knowledge_hits],
    }
    if response.action_proposal is not None:
        data["action_proposal"] = response.action_proposal.model_dump()
    return HomeAssistantAssistResponse(
        response_type="action_confirmation" if response.requires_confirmation else "query_answer",
        speech=escape_markdown_v2(response.answer),
        conversation_id=conversation_id,
        continue_conversation=True,
        requires_confirmation=response.requires_confirmation,
        action_id=action_id,
        action_label=action_label,
        data=data,
    )
