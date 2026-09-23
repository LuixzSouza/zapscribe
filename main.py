import asyncio
import hashlib
import json
import mimetypes
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import local_ai
import transcriber as transcriber_mod
import watcher
from transcriber import DEFAULT_MODEL, MODELS, PARALLEL, PRECISE_MODEL, Transcriber

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

transcriber = Transcriber()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    await transcriber.start()
    watch = asyncio.create_task(watcher.run(import_file))
    yield
    watch.cancel()
    await transcriber.stop()


app = FastAPI(title="Zapscribe", lifespan=lifespan)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LocalOnly:
    """Impede que sites abertos no navegador mexam no Zapscribe.

    - Host diferente de localhost: DNS rebinding (um domínio que aponta para 127.0.0.1
      e conseguiria ler as transcrições).
    - Origin de outro site em envios (POST, PATCH…): CSRF (um site enviando arquivos ou
      apagando áudios sem você saber).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            host = urlparse(f"//{headers.get('host', '')}").hostname
            origin = headers.get("origin")
            if host not in LOCAL_HOSTS:
                return await JSONResponse({"detail": "Acesso permitido só por localhost"}, 400)(scope, receive, send)
            if scope["method"] not in ("GET", "HEAD", "OPTIONS") and origin and urlparse(origin).hostname not in LOCAL_HOSTS:
                return await JSONResponse({"detail": "Origem não permitida"}, 403)(scope, receive, send)
        await self.app(scope, receive, send)


app.add_middleware(LocalOnly)


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
        "device": transcriber_mod.device,
        "downloads": str(watcher.default_folder()),
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


def register_audio(file_name: str, sha256: str, size: int, original: str, recorded_at: float | None,
                   model: str = DEFAULT_MODEL, language: str = "pt", client_id: int | None = None) -> dict:
    """Cria a transcrição de um arquivo já salvo em AUDIO_DIR e o põe na fila.

    Se o mesmo áudio já existe, apaga a cópia e devolve o existente com duplicate=True.
    """
    if (existing := db.find_by_hash(sha256)) is not None:
        (db.AUDIO_DIR / file_name).unlink(missing_ok=True)
        return {**db.get_transcription(existing, full=False), "duplicate": True}

    id_ = db.create_transcription(
        title=initial_title(original),
        original_name=original,
        file_name=file_name,
        size=size,
        recorded_at=recorded_at,
        model=model,
        language=language,
        client_id=client_id,
        sha256=sha256,
    )
    transcriber.enqueue(id_)
    return transcriber.publish_item(id_)


def new_file_name(original: str) -> str:
    return f"{uuid.uuid4().hex}{Path(original).suffix.lower() or '.ogg'}"


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
    file_name = new_file_name(original)
    size = 0
    sha = hashlib.sha256()
    with open(db.AUDIO_DIR / file_name, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            sha.update(chunk)
            size += len(chunk)

    item = register_audio(file_name, sha.hexdigest(), size, original, recorded_at_from(original, last_modified),
                          model, language, client_id)
    # O mesmo áudio enviado de novo: devolve o que já existe em vez de duplicar
    return JSONResponse(item) if item.get("duplicate") else item


def import_file(path: Path) -> dict:
    """Importa um arquivo da pasta vigiada (roda fora do loop principal)."""
    file_name = new_file_name(path.name)
    size = 0
    sha = hashlib.sha256()
    with open(path, "rb") as src, open(db.AUDIO_DIR / file_name, "wb") as out:
        while chunk := src.read(1024 * 1024):
            out.write(chunk)
            sha.update(chunk)
            size += len(chunk)
    return register_audio(file_name, sha.hexdigest(), size, path.name, recorded_at_from(path.name, path.stat().st_mtime * 1000))


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
    vocabulary: str = ""


class ClientPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    phone: str | None = None
    notes: str | None = None
    vocabulary: str | None = None


@app.get("/api/clients")
def list_clients():
    return db.list_clients()


@app.post("/api/clients", status_code=201)
def create_client(body: ClientIn):
    return db.get_client(db.create_client(body.name.strip(), body.phone.strip(), body.notes, body.vocabulary))


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
    values.pop("watch_since", None)  # controlado pelo servidor
    if values.get("watch_enabled") and not db.get_settings()["watch_enabled"]:
        # Ao ligar a importação, só entram os áudios salvos daqui em diante
        values["watch_since"] = time.time()
    return db.save_settings(values)


# ------------------------------------------------------------------ IA local (Ollama)

async def local_model(status: dict) -> str | None:
    """O modelo escolhido nos ajustes, se estiver instalado; senão o menor instalado."""
    names = [m["name"] for m in status["models"]]
    chosen = db.get_settings()["local_model"]
    return chosen if chosen in names else (names[0] if names else None)


@app.get("/api/ai/local")
async def local_status():
    status = await local_ai.status()
    return {**status, "model": await local_model(status)}


class LocalGenerate(BaseModel):
    prompt: str = Field(min_length=1)
    model: str | None = None
    id: int | None = None  # a transcrição onde guardar a resposta (quando é um áudio só)


@app.post("/api/ai/local/generate")
async def local_generate(body: LocalGenerate):
    if body.id is not None and not db.get_transcription(body.id, full=False):
        raise not_found()
    model = body.model or await local_model(await local_ai.status())
    if not model:
        raise HTTPException(409, "Nenhum modelo de IA local instalado")

    stream = local_ai.generate(body.prompt, model)
    try:
        # Espera a primeira parte (inclui carregar o modelo) para poder responder com erro normal
        first = await anext(stream, "")
    except local_ai.LocalAIError as e:
        raise HTTPException(502, str(e))

    async def output():
        parts = [first]
        yield first
        try:
            async for chunk in stream:
                parts.append(chunk)
                yield chunk
        except local_ai.LocalAIError as e:
            yield f"\n\n[Erro da IA local: {e}]"
            return
        # Só guarda a resposta completa (se a pessoa clicou em "Parar", não chega aqui)
        if body.id is not None and db.get_transcription(body.id, full=False):
            db.update_transcription(body.id, ai_result="".join(parts).strip(), ai_model=model)

    return StreamingResponse(output(), media_type="text/plain; charset=utf-8", headers={"X-Model": model})


class LocalPull(BaseModel):
    model: str = Field(min_length=1, max_length=200)


@app.post("/api/ai/local/pull")
async def local_pull(body: LocalPull):
    progress = local_ai.pull(body.model)
    try:
        first = await anext(progress, None)
    except local_ai.LocalAIError as e:
        raise HTTPException(502, str(e))

    async def output():
        if first:
            yield json.dumps(first) + "\n"
        try:
            async for data in progress:
                yield json.dumps(data) + "\n"
        except local_ai.LocalAIError as e:
            yield json.dumps({"error": str(e)}) + "\n"

    return StreamingResponse(output(), media_type="application/x-ndjson")


# ------------------------------------------------------------------ backup

@app.get("/api/backup")
def backup(background: BackgroundTasks):
    """Um .zip com o banco e os áudios. Para restaurar, descompacte na pasta data/."""
    path = db.backup_zip()
    background.add_task(path.unlink, missing_ok=True)
    return FileResponse(path, media_type="application/zip",
                        filename=f"zapscribe-backup-{time.strftime('%Y-%m-%d')}.zip")


@app.exception_handler(sqlite3.IntegrityError)
def integrity_error(_request, exc):
    return JSONResponse({"detail": f"Dados inválidos: {exc}"}, status_code=400)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_browser_when_ready(url: str, port: int) -> None:
    def wait():
        for _ in range(300):
            if port_in_use(port):
                webbrowser.open(url)
                return
            time.sleep(0.2)

    threading.Thread(target=wait, daemon=True).start()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("ZAPSCRIBE_PORT", "8000"))
    url = f"http://127.0.0.1:{port}"
    browser = "--no-browser" not in sys.argv
    if port_in_use(port):
        # Já está rodando (ex.: dois cliques no iniciar.bat): só abre a página
        print(f"O Zapscribe já está aberto em {url}")
        if browser:
            webbrowser.open(url)
        sys.exit(0)
    if browser:
        open_browser_when_ready(url, port)
    uvicorn.run(app, host="127.0.0.1", port=port, timeout_graceful_shutdown=1)
