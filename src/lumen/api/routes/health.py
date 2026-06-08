from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok", "service": "lumen-core"}


@router.get("/health/system")
async def system_healthcheck(request: Request) -> dict:
    container = request.app.state.container
    db_ok = container.database.ping()
    ha_ok = await container.home_assistant.healthcheck()
    ollama_ok = await container.ollama.healthcheck()
    config_ok = bool(container.settings.lumen_db_path and container.settings.home_assistant_url and container.settings.ollama_base_url)
    overall = "ok" if db_ok and config_ok else "degraded"
    return {
        "status": overall,
        "service": "lumen-core",
        "dependencies": {
            "database": "ok" if db_ok else "failed",
            "home_assistant": "ok" if ha_ok else "unavailable",
            "ollama": "ok" if ollama_ok else "unavailable",
            "config": "ok" if config_ok else "invalid",
        },
    }
