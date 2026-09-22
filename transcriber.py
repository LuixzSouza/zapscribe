"""Fila de transcrição em segundo plano, com eventos em tempo real para a interface."""

import asyncio
import logging
import os
import threading
import time
from pathlib import Path

import anyio
from faster_whisper import BatchedInferencePipeline, WhisperModel
from faster_whisper.utils import download_model

import db

log = logging.getLogger("zapscribe")

MODELS_DIR = Path(__file__).parent / "models"

# Modelos disponíveis (do mais rápido ao mais preciso)
MODELS = {
    "small": "Rápido",
    "medium": "Equilibrado",
    "large-v3-turbo": "Preciso",
}
DEFAULT_MODEL = "small"
PRECISE_MODEL = "large-v3-turbo"

# Quantos áudios são transcritos ao mesmo tempo. Os núcleos da CPU são
# divididos entre eles (medido: 3 em paralelo rende ~40% mais que em fila).
PARALLEL = 3
CPU_THREADS = max(1, (os.cpu_count() or 4) // PARALLEL)

_models: dict[str, WhisperModel] = {}
_models_lock = threading.Lock()


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


def auto_title(text: str, limit: int = 60) -> str:
    """Primeiras palavras da transcrição, como o assunto de um e-mail."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:!?") + "…"


class Transcriber:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.cancelled: set[int] = set()
        self.subscribers: set[asyncio.Queue] = set()
        self.tasks: list[asyncio.Task] = []
        self.loop: asyncio.AbstractEventLoop | None = None

    def _on_loop(self, fn, *args) -> None:
        """Executa no loop principal (as rotas síncronas do FastAPI rodam em outras threads)."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            fn(*args)
        else:
            self.loop.call_soon_threadsafe(fn, *args)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        # Retoma o que ficou pendente quando o servidor foi desligado
        for id_ in db.unfinished_ids():
            db.update_transcription(id_, status="queued", progress=0)
            self.queue.put_nowait(id_)
        self.tasks = [asyncio.create_task(self._worker()) for _ in range(PARALLEL)]
        # Carrega o modelo padrão em segundo plano para o primeiro áudio não esperar
        threading.Thread(target=get_model, args=(DEFAULT_MODEL,), daemon=True).start()

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()

    def enqueue(self, id_: int) -> None:
        self.cancelled.discard(id_)
        self._on_loop(self.queue.put_nowait, id_)

    def cancel(self, id_: int) -> None:
        self.cancelled.add(id_)

    # ------------------------------------------------------------ eventos

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, event: dict) -> None:
        self._on_loop(self._publish, event)

    def _publish(self, event: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def publish_item(self, id_: int) -> dict | None:
        item = db.get_transcription(id_, full=False)
        if item:
            self.publish({"type": "item", "item": item})
        return item

    # ------------------------------------------------------------ processamento

    async def _worker(self) -> None:
        while True:
            id_ = await self.queue.get()
            try:
                await self._process(id_)
            except Exception:
                log.exception("Falha inesperada ao transcrever %s", id_)
            finally:
                self.cancelled.discard(id_)

    async def _process(self, id_: int) -> None:
        item = db.get_transcription(id_)
        if not item or item["status"] != "queued" or id_ in self.cancelled:
            return

        db.update_transcription(id_, status="running", progress=0, segments=[], text="", error="", elapsed=0)
        self.publish_item(id_)
        started = time.perf_counter()
        segments_out: list[dict] = []

        try:
            whisper = await anyio.to_thread.run_sync(get_model, item["model"])
            pipeline = BatchedInferencePipeline(whisper)
            path = str(db.AUDIO_DIR / item["file_name"])
            language = None if item["language"] == "auto" else item["language"]
            segments, info = await anyio.to_thread.run_sync(
                lambda: pipeline.transcribe(path, language=language, batch_size=8, beam_size=1)
            )
            db.update_transcription(id_, duration=info.duration, detected_language=info.language)
            self.publish_item(id_)

            while True:
                seg = await anyio.to_thread.run_sync(next, segments, None)
                if seg is None:
                    break
                if id_ in self.cancelled:
                    return
                segment = {"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()}
                segments_out.append(segment)
                progress = min(1.0, seg.end / info.duration) if info.duration else 0
                db.update_transcription(id_, segments=segments_out, progress=progress)
                self.publish({
                    "type": "segment",
                    "id": id_,
                    "index": len(segments_out) - 1,
                    "segment": segment,
                    "progress": progress,
                })

            text = " ".join(s["text"] for s in segments_out if s["text"])
            fields = dict(status="done", progress=1, text=text, segments=segments_out,
                          elapsed=time.perf_counter() - started)
            current = db.get_transcription(id_, full=False)
            if current and current["title_auto"] and text:
                fields["title"] = auto_title(text)
            db.update_transcription(id_, **fields)
        except Exception as e:
            log.exception("Erro ao transcrever %s", id_)
            db.update_transcription(id_, status="error", error=str(e), elapsed=time.perf_counter() - started)

        self.publish_item(id_)
