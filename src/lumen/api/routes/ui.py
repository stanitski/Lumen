from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["ui"])


@router.get("/", include_in_schema=False)
@router.get("/ui", include_in_schema=False)
def admin_ui() -> FileResponse:
    static_path = Path(__file__).resolve().parents[2] / "static" / "admin.html"
    return FileResponse(static_path)
