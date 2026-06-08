import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import ctypes

from fastapi import APIRouter, Depends, HTTPException

from lumen.api.deps import get_container
from lumen.models import KnowledgeUploadRequest, MemoryFactPayload, MemoryFactTextPayload, ReindexRequest

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/reindex")
def reindex(payload: ReindexRequest, container: Any = Depends(get_container)) -> dict:
    paths = payload.paths or container.settings.knowledge_path_list
    indexed = 0
    for path in paths:
        indexed += container.knowledge_store.ingest_path(path)
    return {"status": "ok", "indexed_documents": indexed, "paths": paths}


@router.post("/bootstrap-home-assistant")
async def bootstrap_home_assistant(container: Any = Depends(get_container)) -> dict:
    snapshot_dir = container.settings.knowledge_path_list[0] if container.settings.knowledge_path_list else "./data/knowledge"
    return await container.bootstrap_service.sync_home_assistant_snapshot(snapshot_dir)


@router.get("/summary")
async def admin_summary(container: Any = Depends(get_container)) -> dict:
    db_ok = container.database.ping()
    ha_ok = await container.home_assistant.healthcheck()
    ollama_ok = await container.ollama.healthcheck()
    with container.database.session() as connection:
        memory_count = int(connection.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0])
        conversation_count = int(connection.execute("SELECT COUNT(*) FROM conversation_logs").fetchone()[0])
        knowledge_doc_count = int(connection.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0])
        knowledge_chunk_count = int(connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0])
        ingestion_count = int(connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0])
        last_ingestion = connection.execute(
            """
            SELECT source_path, status, documents_indexed, message, created_at
            FROM ingestion_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "service": "lumen-core",
        "status": "ok" if db_ok else "degraded",
        "database_path": container.settings.lumen_db_path,
        "current_model": container.settings.ollama_model,
        "knowledge_paths": container.settings.knowledge_path_list,
        "dependencies": {
            "database": "ok" if db_ok else "failed",
            "home_assistant": "ok" if ha_ok else "unavailable",
            "ollama": "ok" if ollama_ok else "unavailable",
        },
        "counts": {
            "memory_facts": memory_count,
            "conversation_logs": conversation_count,
            "knowledge_documents": knowledge_doc_count,
            "knowledge_chunks": knowledge_chunk_count,
            "ingestion_runs": ingestion_count,
        },
        "last_ingestion": dict(last_ingestion) if last_ingestion else None,
    }


@router.get("/host/telemetry")
def host_telemetry() -> dict:
    ram = _read_ram_stats()
    gpu = _read_gpu_stats()
    disk = shutil.disk_usage(Path.cwd().anchor or "C:\\")
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "host": {
            "hostname": socket.gethostname(),
            "python": sys.version.split()[0],
        },
        "ram": ram,
        "gpu": gpu,
        "disk": {
            "total_gb": round(disk.total / (1024**3), 1),
            "used_gb": round((disk.total - disk.free) / (1024**3), 1),
            "free_gb": round(disk.free / (1024**3), 1),
        },
    }


@router.get("/database/overview")
def database_overview(container: Any = Depends(get_container), limit: int = 20) -> dict:
    safe_limit = max(1, min(limit, 100))
    with container.database.session() as connection:
        table_counts = {
            "schema_version": int(connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]),
            "conversation_logs": int(connection.execute("SELECT COUNT(*) FROM conversation_logs").fetchone()[0]),
            "memory_facts": int(connection.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0]),
            "action_traces": int(connection.execute("SELECT COUNT(*) FROM action_traces").fetchone()[0]),
            "knowledge_documents": int(connection.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]),
            "knowledge_chunks": int(connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]),
            "ingestion_runs": int(connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]),
        }
        recent_memory = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, category, subject, predicate, value, confidence, importance, source_ref, last_seen
                FROM memory_facts
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        ]
        recent_documents = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, source_type, source_ref, title, updated_at
                FROM knowledge_documents
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        ]
        recent_ingestions = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, source_path, status, documents_indexed, message, created_at
                FROM ingestion_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        ]
    return {
        "database_path": container.settings.lumen_db_path,
        "table_counts": table_counts,
        "recent_memory": recent_memory,
        "recent_documents": recent_documents,
        "recent_ingestions": recent_ingestions,
    }


@router.get("/memory/facts")
def list_memory_facts(container: Any = Depends(get_container), limit: int = 100) -> dict:
    return {"items": container.memory_store.list_facts(limit=limit)}


@router.post("/memory/facts")
def create_memory_fact(payload: MemoryFactPayload, container: Any = Depends(get_container)) -> dict:
    fact_id = container.memory_store.add_fact(
        category=payload.category.strip(),
        subject=payload.subject.strip(),
        predicate=payload.predicate.strip(),
        value=payload.value.strip(),
        confidence=float(payload.confidence),
        importance=int(payload.importance),
        source_ref=payload.source_ref.strip() or "admin:manual",
        tags=payload.tags,
        expires_at=_parse_optional_datetime(payload.expires_at),
    )
    fact = container.memory_store.get_fact(fact_id)
    return {"status": "ok", "item": fact}


@router.post("/memory/facts/from-text")
def create_memory_fact_from_text(payload: MemoryFactTextPayload, container: Any = Depends(get_container)) -> dict:
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Memory text is required.")

    source_ref = payload.source_ref.strip() or "admin:manual"
    created_ids = container.memory_store.extract_facts_from_text(text, source_ref)
    if not created_ids:
        fallback_id = container.memory_store.add_fact(
            category="rule",
            subject="user",
            predicate="remember",
            value=text,
            confidence=0.75,
            importance=6,
            source_ref=source_ref,
            tags=["manual", "rule"],
            expires_at=None,
        )
        created_ids = [fallback_id]

    items = [container.memory_store.get_fact(fact_id) for fact_id in created_ids]
    return {"status": "ok", "items": [item for item in items if item is not None]}


@router.put("/memory/facts/{fact_id}")
def update_memory_fact(fact_id: int, payload: MemoryFactPayload, container: Any = Depends(get_container)) -> dict:
    fact = container.memory_store.update_fact(
        fact_id,
        category=payload.category.strip(),
        subject=payload.subject.strip(),
        predicate=payload.predicate.strip(),
        value=payload.value.strip(),
        confidence=float(payload.confidence),
        importance=int(payload.importance),
        source_ref=payload.source_ref.strip() or "admin:manual",
        tags=payload.tags,
        expires_at=_parse_optional_datetime(payload.expires_at),
    )
    if fact is None:
        raise HTTPException(status_code=404, detail="Memory fact not found.")
    return {"status": "ok", "item": fact}


@router.delete("/memory/facts/{fact_id}")
def delete_memory_fact(fact_id: int, container: Any = Depends(get_container)) -> dict:
    deleted = container.memory_store.delete_fact(fact_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory fact not found.")
    return {"status": "ok", "deleted_id": fact_id}


@router.post("/knowledge/upload")
def upload_knowledge(payload: KnowledgeUploadRequest, container: Any = Depends(get_container)) -> dict:
    roots = container.settings.knowledge_path_list
    if not roots:
        raise HTTPException(status_code=400, detail="KNOWLEDGE_PATHS is empty.")

    filename = Path(payload.filename).name.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Filename is required.")
    if not re.fullmatch(r"[\w.\- ]+", filename):
        raise HTTPException(status_code=400, detail="Filename contains unsupported characters.")

    root_path = Path(roots[0]).resolve()
    relative_dir = Path(payload.relative_path.strip()) if payload.relative_path else Path()
    if relative_dir.is_absolute() or ".." in relative_dir.parts:
        raise HTTPException(status_code=400, detail="relative_path must stay inside the knowledge root.")

    target_dir = (root_path / relative_dir).resolve()
    if target_dir != root_path and root_path not in target_dir.parents:
        raise HTTPException(status_code=400, detail="Resolved target path is outside the knowledge root.")
    target_dir.mkdir(parents=True, exist_ok=True)

    target_file = target_dir / filename
    target_file.write_text(payload.content, encoding="utf-8")

    indexed = 0
    if payload.reindex_after_upload:
        indexed = container.knowledge_store.ingest_path(str(target_file))

    return {
        "status": "ok",
        "saved_to": str(target_file),
        "indexed_documents": indexed,
        "knowledge_root": str(root_path),
    }


@router.get("/knowledge/documents/{document_id}")
def get_knowledge_document(document_id: int, container: Any = Depends(get_container)) -> dict:
    with container.database.session() as connection:
        row = connection.execute(
            """
            SELECT id, source_type, source_ref, title, content, updated_at
            FROM knowledge_documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found.")
    return {"item": dict(row)}


@router.delete("/knowledge/documents/{document_id}")
def delete_knowledge_document(document_id: int, container: Any = Depends(get_container)) -> dict:
    with container.database.session() as connection:
        row = connection.execute(
            "SELECT id, source_ref FROM knowledge_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Knowledge document not found.")

        source_ref = str(row["source_ref"] or "").strip()
        file_deleted = False
        if source_ref:
            source_path = Path(source_ref).resolve()
            knowledge_roots = [Path(path).resolve() for path in container.settings.knowledge_path_list]
            if any(source_path == root or root in source_path.parents for root in knowledge_roots):
                if source_path.exists() and source_path.is_file():
                    source_path.unlink()
                    file_deleted = True

        connection.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))
        connection.commit()

    return {"status": "ok", "deleted_id": document_id, "file_deleted": file_deleted}


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="expires_at must be a valid ISO datetime.") from exc


def _read_ram_stats() -> dict:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_phys", ctypes.c_ulonglong),
            ("avail_phys", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("avail_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("avail_virtual", ctypes.c_ulonglong),
            ("avail_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) == 0:
        return {"status": "unavailable"}

    used = status.total_phys - status.avail_phys
    return {
        "status": "ok",
        "total_gb": round(status.total_phys / (1024**3), 1),
        "used_gb": round(used / (1024**3), 1),
        "available_gb": round(status.avail_phys / (1024**3), 1),
        "percent": int(status.memory_load),
    }


def _read_gpu_stats() -> dict:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,temperature.gpu,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.check_output(command, text=True, timeout=2).strip()
    except Exception:
        return {"status": "unavailable"}

    if not output:
        return {"status": "unavailable"}

    first_line = output.splitlines()[0]
    parts = [part.strip() for part in first_line.split(",")]
    if len(parts) < 5:
        return {"status": "unavailable"}

    total_mb = float(parts[1])
    used_mb = float(parts[2])
    return {
        "status": "ok",
        "name": parts[0],
        "total_gb": round(total_mb / 1024, 1),
        "used_gb": round(used_mb / 1024, 1),
        "temperature_c": int(float(parts[3])),
        "utilization_percent": int(float(parts[4])),
    }
