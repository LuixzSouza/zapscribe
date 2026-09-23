"""Fila de transcrição em segundo plano, com eventos em tempo real para a interface."""

import asyncio
import logging
import os
import threading
import time
from pathlib import Path

import anyio
import av
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

# Durante a transcrição, o texto parcial é gravado no banco no máximo a cada N segundos
# (a interface recebe cada trecho na hora pelos eventos)
SAVE_INTERVAL = 2.0


def _detect_device() -> str:
    """Usa a placa de vídeo NVIDIA quando houver; ZAPSCRIBE_DEVICE=cpu|cuda força uma opção."""
    forced = os.environ.get("ZAPSCRIBE_DEVICE", "").lower()
    if forced in ("cpu", "cuda"):
        return forced
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


device = _detect_device()
_models: dict[str, WhisperModel] = {}
_models_lock = threading.Lock()


def get_model(name: str) -> WhisperModel:
    with _models_lock:
        if name not in _models:
            # Baixa para uma pasta local (sem symlinks, que falham no Windows sem admin)
            path = download_model(name, output_dir=str(MODELS_DIR / name))
            if device == "cuda":
                _models[name] = WhisperModel(path, device="cuda", compute_type="float16", num_workers=PARALLEL)
            else:
                _models[name] = WhisperModel(
                    path,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=CPU_THREADS,
                    num_workers=PARALLEL,
                )
        return _models[name]


def fall_back_to_cpu() -> None:
    """A GPU foi detectada mas não funcionou (ex.: faltam as bibliotecas CUDA): segue na CPU."""
    global device
    with _models_lock:
        device = "cpu"
        _models.clear()


def vocabulary_for(item: dict) -> str | None:
    """Vocabulário geral + o do cliente, para o Whisper reconhecer nomes e termos."""
    parts = [db.get_settings().get("vocabulary") or ""]
    if item.get("client_id") and (client := db.get_client(item["client_id"])):
        parts.append(client.get("vocabulary") or "")
    words = [w.strip() for part in parts for w in part.replace("\n", ",").split(",") if w.strip()]
    return ", ".join(dict.fromkeys(words)) or None


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
            hotwords = vocabulary_for(item)
            segments, info = await anyio.to_thread.run_sync(
                lambda: pipeline.transcribe(path, language=language, batch_size=8, beam_size=1, hotwords=hotwords)
            )
            db.update_transcription(id_, duration=info.duration, detected_language=info.language)
            self.publish_item(id_)

            last_save = time.monotonic()
            while True:
                seg = await anyio.to_thread.run_sync(next, segments, None)
                if seg is None:
                    break
                if id_ in self.cancelled:
                    return
                segment = {"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()}
                segments_out.append(segment)
                progress = min(1.0, seg.end / info.duration) if info.duration else 0
                if time.monotonic() - last_save >= SAVE_INTERVAL:
                    db.update_transcription(id_, segments=segments_out, progress=progress)
                    last_save = time.monotonic()
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
            if id_ in self.cancelled:
                return  # excluído durante a transcrição: o arquivo já foi apagado
            if device == "cuda" and not isinstance(e, av.error.FFmpegError):
                log.warning("Falha na GPU (%s). Continuando na CPU.", e)
                fall_back_to_cpu()
                db.update_transcription(id_, status="queued", progress=0)
                self.queue.put_nowait(id_)
                self.publish_item(id_)
                return
            log.exception("Erro ao transcrever %s", id_)
            error = str(e)
            if isinstance(e, av.error.FFmpegError):
                error = "Não foi possível ler este arquivo. Ele pode estar corrompido ou não ser um áudio."
            db.update_transcription(id_, status="error", error=error, elapsed=time.perf_counter() - started)

        self.publish_item(id_)
