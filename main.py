import asyncio
import json
import mimetypes
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
from transcriber import DEFAULT_MODEL, MODELS, PARALLEL, PRECISE_MODEL, Transcriber

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

transcriber = Transcriber()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    await transcriber.start()
    yield
    await transcriber.stop()


app = FastAPI(title="Zapscribe", lifespan=lifespan)


def not_found(what: str = "Transcrição"):
    return HTTPException(404, f"{what} não encontrada")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def config():
    return {
        "models": MODELS,
        "default": DEFAULT_MODEL,
        "precise": PRECISE_MODEL,
        "parallel": PARALLEL,
    }


# ------------------------------------------------------------------ eventos (SSE)

@app.get("/api/events")
async def events(request: Request):
    queue = transcriber.subscribe()

    async def stream():
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            transcriber.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ transcrições

WHATSAPP_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\D+?(\d{2})\.(\d{2})(?:\.(\d{2}))?")


def recorded_at_from(filename: str, last_modified_ms: float | None) -> float | None:
    """Data do áudio: a do nome do arquivo do WhatsApp ou a data de modificação."""
    if "whatsapp" in filename.lower() and (m := WHATSAPP_DATE.search(filename)):
        y, mo, d, h, mi, s = (int(x or 0) for x in m.groups())
        return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
    return last_modified_ms / 1000 if last_modified_ms else None


def initial_title(filename: str) -> str:
    if "whatsapp" in filename.lower():
        return "Áudio do WhatsApp"
    return Path(filename).stem


@app.get("/api/transcriptions")
def list_transcriptions():
    return db.list_transcriptions()


@app.post("/api/transcriptions", status_code=201)
async def create_transcription(
    file: UploadFile = File(...),
    model: str = Form(DEFAULT_MODEL),
    language: str = Form("pt"),
    client_id: int | None = Form(None),
    last_modified: float | None = Form(None),
):
    if model not in MODELS:
        raise HTTPException(400, "Modelo inválido")
    if client_id is not None and not db.get_client(client_id):
        raise not_found("Cliente")

    original = file.filename or "audio.ogg"
    file_name = f"{uuid.uuid4().hex}{Path(original).suffix.lower() or '.ogg'}"
    size = 0
    with open(db.AUDIO_DIR / file_name, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            size += len(chunk)

    id_ = db.create_transcription(
        title=initial_title(original),
        original_name=original,
        file_name=file_name,
        size=size,
        recorded_at=recorded_at_from(original, last_modified),
        model=model,
        language=language,
        client_id=client_id,
    )
    transcriber.enqueue(id_)
    return transcriber.publish_item(id_)


@app.get("/api/transcriptions/{id_}")
def get_transcription(id_: int):
    item = db.get_transcription(id_)
    if not item:
        raise not_found()
    item.pop("file_name")
    return item


class Segment(BaseModel):
    start: float
    end: float
    text: str


class TranscriptionPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    client_id: int | None = None
    notes: str | None = None
    resolved: bool | None = None
    segments: list[Segment] | None = None


@app.patch("/api/transcriptions/{id_}")
def update_transcription(id_: int, patch: TranscriptionPatch):
    item = db.get_transcription(id_, full=False)
    if not item:
        raise not_found()

    fields = patch.model_dump(exclude_unset=True)
    if "title" in fields:
        fields["title_auto"] = False
    if fields.get("client_id") is not None and not db.get_client(fields["client_id"]):
        raise not_found("Cliente")
    if "segments" in fields:
        if item["status"] != "done":
            raise HTTPException(409, "Aguarde a transcrição terminar para editar o texto")
        fields["text"] = " ".join(s["text"].strip() for s in fields["segments"] if s["text"].strip())

    db.update_transcription(id_, **fields)
    return transcriber.publish_item(id_)


def remove_transcription(id_: int) -> None:
    transcriber.cancel(id_)
    file_name = db.delete_transcription(id_)
    if file_name:
        (db.AUDIO_DIR / file_name).unlink(missing_ok=True)


@app.delete("/api/transcriptions/{id_}", status_code=204)
def delete_transcription(id_: int):
    if not db.get_transcription(id_, full=False):
        raise not_found()
    remove_transcription(id_)
    transcriber.publish({"type": "deleted", "ids": [id_]})


class Retranscribe(BaseModel):
    model: str = PRECISE_MODEL
    language: str | None = None


@app.post("/api/transcriptions/{id_}/retranscribe")
def retranscribe(id_: int, body: Retranscribe):
    item = db.get_transcription(id_, full=False)
    if not item:
        raise not_found()
    if body.model not in MODELS:
        raise HTTPException(400, "Modelo inválido")
    if item["status"] in ("queued", "running"):
        raise HTTPException(409, "Este áudio já está sendo transcrito")

    db.update_transcription(
        id_, status="queued", progress=0, model=body.model, error="",
        language=body.language or item["language"],
    )
    transcriber.enqueue(id_)
    return transcriber.publish_item(id_)


class Bulk(BaseModel):
    ids: list[int] = Field(min_length=1)
    action: Literal["delete", "resolve", "unresolve", "set_client"]
    client_id: int | None = None


@app.post("/api/transcriptions/bulk")
def bulk(body: Bulk):
    if body.action == "set_client" and body.client_id is not None and not db.get_client(body.client_id):
        raise not_found("Cliente")

    for id_ in body.ids:
        if body.action == "delete":
            remove_transcription(id_)
        elif body.action in ("resolve", "unresolve"):
            db.update_transcription(id_, resolved=body.action == "resolve")
        else:
            db.update_transcription(id_, client_id=body.client_id)

    if body.action == "delete":
        transcriber.publish({"type": "deleted", "ids": body.ids})
    else:
        for id_ in body.ids:
            transcriber.publish_item(id_)
    return {"ok": True}


@app.get("/api/transcriptions/{id_}/audio")
def transcription_audio(id_: int):
    item = db.get_transcription(id_)
    if not item:
        raise not_found()
    path = db.AUDIO_DIR / item["file_name"]
    media_type = mimetypes.guess_type(item["original_name"])[0] or "audio/ogg"
    return FileResponse(path, media_type=media_type)


# ------------------------------------------------------------------ clientes

class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = ""
    notes: str = ""


class ClientPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    phone: str | None = None
    notes: str | None = None


@app.get("/api/clients")
def list_clients():
    return db.list_clients()


@app.post("/api/clients", status_code=201)
def create_client(body: ClientIn):
    return db.get_client(db.create_client(body.name.strip(), body.phone.strip(), body.notes))


@app.patch("/api/clients/{id_}")
def update_client(id_: int, body: ClientPatch):
    if not db.get_client(id_):
        raise not_found("Cliente")
    db.update_client(id_, **body.model_dump(exclude_unset=True))
    return db.get_client(id_)


@app.delete("/api/clients/{id_}", status_code=204)
def delete_client(id_: int):
    if not db.get_client(id_):
        raise not_found("Cliente")
    db.delete_client(id_)


# ------------------------------------------------------------------ modelos de prompt

class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)


class TemplatePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    body: str | None = Field(None, min_length=1)


@app.get("/api/templates")
def list_templates():
    return db.list_templates()


@app.post("/api/templates", status_code=201)
def create_template(body: TemplateIn):
    return db.get_template(db.create_template(body.name.strip(), body.body))


@app.patch("/api/templates/{id_}")
def update_template(id_: int, body: TemplatePatch):
    if not db.get_template(id_):
        raise not_found("Modelo")
    db.update_template(id_, **body.model_dump(exclude_unset=True))
    return db.get_template(id_)


@app.delete("/api/templates/{id_}", status_code=204)
def delete_template(id_: int):
    if not db.get_template(id_):
        raise not_found("Modelo")
    db.delete_template(id_)


# ------------------------------------------------------------------ ajustes

@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def save_settings(values: dict):
    return db.save_settings(values)


@app.exception_handler(sqlite3.IntegrityError)
def integrity_error(_request, exc):
    return JSONResponse({"detail": f"Dados inválidos: {exc}"}, status_code=400)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, timeout_graceful_shutdown=1)
