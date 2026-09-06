"""FastAPI-бэкенд «Судебного симулятора» (этап 2).

REST + WebSocket, все данные в памяти (см. session_store.py):

- ``POST /api/setup``         — настройка роутера/моделей/юрисдикции, создание сессии;
- ``POST /api/upload/{id}``   — документы дела (multipart) + промпт-контекст текстом;
- ``GET  /api/session/{id}``  — статус и прогресс сессии;
- ``POST /api/run/{id}``      — запуск симуляции в фоновом потоке;
- ``WS   /ws/session/{id}``   — живой поток событий DebateEvent (JSON) + replay;
- ``GET  /api/report/{id}``   — итоговый отчёт (?format=md|json);
- ``GET  /api/report/{id}/download`` — скачать markdown-файл отчёта;
- ``GET  /api/health``        — проверка живости (для install.bat).

CLI (src/main.py) остаётся debug-режимом: оба пути используют один graph.py.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from .config import Config
from .document_loader import SUPPORTED_EXTENSIONS
from .session_store import (
    SESSIONS_DIR,
    STATUS_CREATED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_READY,
    Session,
    SessionStore,
    run_session_in_thread,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Судебный симулятор API", version="0.1.0")

# Фронтенд (Next.js dev :3000) ходит к бэкенду с другого порта — нужен CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

store = SessionStore()


# --- модели запросов ---------------------------------------------------------

class SetupRequest(BaseModel):
    """Параметры симуляции из веб-формы «Настройка»."""

    base_url: str = Field(min_length=8, description="OpenAI-совместимый роутер, напр. https://routerai.ru/api/v1")
    api_key: str = Field(default="", description="Ключ API; пустое значение допустимо, но /api/run вернёт ошибку")
    model_claimant_lawyer: str = Field(min_length=1)
    model_defendant_lawyer: str = Field(min_length=1)
    model_judge: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=3, description="Напр.: «Российская Федерация, гражданское право»")
    max_rounds: int = Field(default=3, ge=1, le=10)
    max_context_tokens: int = Field(default=12_000, ge=1_000, le=2_000_000)
    target_side: Literal["claimant", "defendant"] = "claimant"
    llm_params: dict[str, Any] = Field(default_factory=dict, description="Доп. параметры запроса (reasoning и т.п.)")

    @field_validator("base_url")
    @classmethod
    def _base_url_scheme(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url должен начинаться с http:// или https://")
        return value

    @field_validator("model_claimant_lawyer", "model_defendant_lawyer", "model_judge", "jurisdiction")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


def _build_config(request: SetupRequest, session_dir: Path) -> Config:
    """Конфиг сессии: поля формы + project_root каталога сессии."""
    return Config(
        api_base_url=request.base_url,
        api_key=request.api_key.strip(),
        api_key_source="web-setup",
        model_claimant_lawyer=request.model_claimant_lawyer,
        model_defendant_lawyer=request.model_defendant_lawyer,
        model_judge=request.model_judge,
        jurisdiction=request.jurisdiction,
        max_rounds=request.max_rounds,
        max_context_tokens=request.max_context_tokens,
        llm_params=dict(request.llm_params),
        project_root=session_dir,
    )


def _get_session_or_404(session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Сессия не найдена: {session_id}")
    return session


# --- эндпоинты ---------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    """Живость сервера (используется install.bat и тестами)."""
    return {"status": "ok"}


@app.post("/api/setup")
def setup(request: SetupRequest) -> dict:
    """Создать сессию с настройками из веб-формы (материалы загружаются отдельно)."""

    def config_factory(session_dir: Path) -> Config:
        return _build_config(request, session_dir)

    session = store.create(target_side=request.target_side, config_factory=config_factory)
    return {
        "session_id": session.id,
        "status": session.status,
        "target_side": session.target_side,
    }


@app.post("/api/upload/{session_id}")
async def upload_case(
    session_id: str,
    files: list[UploadFile] = File(default=[]),
    context: str = Form(default=""),
) -> dict:
    """Загрузить документы дела (PDF/DOCX/TXT/MD) и/или промпт-контекст текстом."""
    session = _get_session_or_404(session_id)
    if session.status == STATUS_RUNNING:
        raise HTTPException(status_code=409, detail="Симуляция уже выполняется — материалы менять нельзя.")

    saved: list[str] = []
    rejected: list[dict] = []
    case_dir = session.config.case_files_dir
    case_dir.mkdir(parents=True, exist_ok=True)
    for upload_file in files or []:
        name = Path(upload_file.filename or "").name  # только имя, без пути
        if not name:
            rejected.append({"file": "", "reason": "пустое имя файла"})
            continue
        if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            rejected.append(
                {"file": name, "reason": f"формат не поддерживается ({', '.join(sorted(SUPPORTED_EXTENSIONS))})"}
            )
            continue
        data = await upload_file.read()
        if not data.strip():
            rejected.append({"file": name, "reason": "файл пуст"})
            continue
        (case_dir / name).write_bytes(data)
        saved.append(name)
        logger.info("Сессия %s: сохранён документ %s (%d байт).", session_id, name, len(data))

    context_saved = False
    if context.strip():
        session.config.case_context_file.write_text(context, encoding="utf-8")
        context_saved = True

    if (saved or context_saved) and session.status == STATUS_CREATED:
        session.status = STATUS_READY
    return {
        "saved_files": saved,
        "rejected_files": rejected,
        "context_saved": context_saved,
        "status": session.status,
    }


@app.get("/api/session/{session_id}")
def session_info(session_id: str) -> dict:
    """Статус и прогресс сессии (fallback для клиентов без WebSocket)."""
    return _get_session_or_404(session_id).public_info()


@app.post("/api/run/{session_id}")
def run_debate_endpoint(session_id: str) -> dict:
    """Запустить симуляцию прений в фоновом потоке (неблокирующе)."""
    session = _get_session_or_404(session_id)
    if session.status == STATUS_RUNNING:
        raise HTTPException(status_code=409, detail="Симуляция уже выполняется.")
    if not session.config.api_key:
        raise HTTPException(
            status_code=400, detail="Ключ API не задан: повторите настройку (POST /api/setup)."
        )
    if not session.config.case_context_file.exists():
        raise HTTPException(
            status_code=400,
            detail="Материалы дела не загружены: вызовите POST /api/upload/{session_id}.",
        )
    run_session_in_thread(session)
    return {"session_id": session.id, "status": session.status}


@app.websocket("/ws/session/{session_id}")
async def ws_session(websocket: WebSocket, session_id: str) -> None:
    """Живой поток событий симуляции.

    События буферизуются в сессии: при переподключении клиент получает их
    с начала (replay), затем — новые по мере генерации. Поток закрывается
    после ``debate_done``/``error``.
    """
    await websocket.accept()
    session = store.get(session_id)
    if session is None:
        await websocket.send_json({"type": "error", "payload": {"message": f"Сессия не найдена: {session_id}"}})
        await websocket.close()
        return

    await websocket.send_json(
        {"type": "status", "payload": {"status": session.status, "target_side": session.target_side}}
    )
    index = 0
    try:
        while True:
            events = session.events
            while index < len(events):
                await websocket.send_json(events[index].as_dict())
                index += 1
            if session.status in (STATUS_DONE, STATUS_ERROR):
                break
            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        logger.info("WS-клиент отключился (сессия %s, доставлено %d событий).", session_id, index)
        return
    await websocket.close()


@app.get("/api/report/{session_id}")
def report(session_id: str, format: str = "md") -> Any:
    """Итоговый отчёт: ``?format=md`` (по умолчанию) или ``?format=json``."""
    session = _get_session_or_404(session_id)
    if session.status != STATUS_DONE or session.result is None:
        raise HTTPException(status_code=409, detail=f"Отчёт ещё не готов (статус: {session.status}).")

    if format == "md":
        return PlainTextResponse(session.report_md or "", media_type="text/markdown; charset=utf-8")
    if format == "json":
        result = session.result
        rec = result.recommendations
        return {
            **session.public_info(),
            "params": {
                "jurisdiction": session.config.jurisdiction,
                "models": {
                    "claimant_lawyer": session.config.model_claimant_lawyer,
                    "defendant_lawyer": session.config.model_defendant_lawyer,
                    "judge": session.config.model_judge,
                },
                "max_rounds": session.config.max_rounds,
                "rounds_played": result.rounds_played,
                "finished_by_judge": result.finished_by_judge,
            },
            "history": [
                {
                    "speaker": statement.speaker,
                    "speaker_title": statement.speaker_title,
                    "round": statement.round,
                    "text": statement.text,
                }
                for statement in result.history
            ],
            "verdict": result.verdict,
            "verified_norms": [
                {
                    "title": norm.title,
                    "number": norm.number,
                    "date": norm.date,
                    "url": norm.source_url,
                }
                for norm in result.verified_norms
            ],
            "legal_warning": result.legal_warning,
            "recommendations": None
            if rec is None
            else {
                "target_side": rec.target_side,
                "side_title": rec.side_title,
                "prospects": rec.prospects,
                "text": rec.text,
            },
            "report_file": str(session.report_path) if session.report_path else None,
        }
    raise HTTPException(status_code=400, detail="format должен быть md или json")


@app.get("/api/report/{session_id}/download")
def report_download(session_id: str) -> FileResponse:
    """Скачать markdown-файл отчёта (PDF — экспорт через печать браузера)."""
    session = _get_session_or_404(session_id)
    if session.report_path is None or not session.report_path.exists():
        raise HTTPException(status_code=409, detail="Отчёт ещё не готов.")
    return FileResponse(
        session.report_path,
        media_type="text/markdown; charset=utf-8",
        filename=session.report_path.name,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
