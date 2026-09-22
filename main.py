import asyncio
import json
import os
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import BatchedInferencePipeline, WhisperModel
from faster_whisper.utils import download_model

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
MODELS_DIR = BASE_DIR / "models"

# Modelos disponíveis (do mais rápido ao mais preciso)
MODELS = {
    "small": "Rápido",
    "medium": "Equilibrado",
    "large-v3-turbo": "Mais preciso",
}
DEFAULT_MODEL = "small"
PRECISE_MODEL = "large-v3-turbo"

# Quantos áudios são transcritos ao mesmo tempo. Os núcleos da CPU são
# divididos entre eles (medido: 3 em paralelo rende ~40% mais que em fila).
PARALLEL = 3
CPU_THREADS = max(1, (os.cpu_count() or 4) // PARALLEL)

_models: dict[str, WhisperModel] = {}
_models_lock = threading.Lock()
_slots = asyncio.Semaphore(PARALLEL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Carrega o modelo padrão em segundo plano para o primeiro áudio não esperar
    threading.Thread(target=get_model, args=(DEFAULT_MODEL,), daemon=True).start()
    yield


app = FastAPI(title="Áudio para Texto", lifespan=lifespan)


def get_model(name: str) -> WhisperModel:
    with _models_lock:
        if name not in _models:
            # Baixa para uma pasta local (sem symlinks, que falham no Windows sem admin)
            path = download_model(name, output_dir=str(MODELS_DIR / name))
            _models[name] = WhisperModel(
                path,
                device="cpu",
                compute_type="int8",
                cpu_threads=CPU_THREADS,
                num_workers=PARALLEL,
            )
        return _models[name]


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


def event(type_: str, **data) -> str:
    return json.dumps({"type": type_, **data}) + "\n"


@app.post("/api/transcribe")
async def transcribe(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(DEFAULT_MODEL),
    language: str = Form("pt"),
):
    if model not in MODELS:
        raise HTTPException(400, "Modelo inválido")

    suffix = Path(file.filename or "audio").suffix or ".ogg"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        while chunk := await file.read(1024 * 1024):
            tmp.write(chunk)
    finally:
        tmp.close()

    async def stream():
        try:
            if _slots.locked():
                yield event("status", message="Na fila...")
            async with _slots:
                started = time.perf_counter()
                if model not in _models:
                    yield event("status", message="Carregando modelo...")
                whisper = await anyio.to_thread.run_sync(get_model, model)
                yield event("status", message="Transcrevendo...")

                pipeline = BatchedInferencePipeline(whisper)
                segments, info = await anyio.to_thread.run_sync(
                    lambda: pipeline.transcribe(
                        tmp.name,
                        language=None if language == "auto" else language,
                        batch_size=8,
                        beam_size=1,
                    )
                )
                yield event("info", language=info.language, duration=info.duration)

                while True:
                    seg = await anyio.to_thread.run_sync(next, segments, None)
                    if seg is None:
                        break
                    # Para de gastar CPU se o usuário removeu o áudio
                    if await request.is_disconnected():
                        return
                    yield event("segment", start=seg.start, end=seg.end, text=seg.text.strip())

                yield event("done", elapsed=time.perf_counter() - started)
        except Exception as e:
            yield event("error", message=str(e))
        finally:
            os.unlink(tmp.name)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
