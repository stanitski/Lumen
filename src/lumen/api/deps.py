from typing import Any

from fastapi import Request


def get_container(request: Request) -> Any:
    return request.app.state.container
