"""Texto para áudio: vozes neurais da Microsoft (online) e Kokoro (offline, no computador).

As vozes online soam mais naturais, mas mandam o texto para a Microsoft. Sem internet, se a voz
offline estiver baixada, ela é usada no lugar. O áudio sai em .ogg Opus, o formato do WhatsApp.

Textos longos são divididos em partes (no fim das frases), geradas em paralelo e juntadas num
áudio só.

Há vozes em português e em inglês. O idioma do texto é reconhecido sozinho: um texto em inglês
é lido pela voz em inglês equivalente à escolhida (e vice-versa), em vez de sair com sotaque.
"""

import asyncio
import hashlib
import io
import logging
import os
import re
import textwrap
import threading
import time
import uuid
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import av
import httpx
import numpy as np

import db
from transcriber import MODELS_DIR

log = logging.getLogger("zapscribe")

# Vozes online (Microsoft Edge). Precisam de internet e mandam o texto para a Microsoft.
ONLINE = os.environ.get("ZAPSCRIBE_TTS_ONLINE", "1") != "0"

VOICES = {
    "pt-BR-AntonioNeural": {"name": "Antonio", "engine": "online", "gender": "m", "lang": "pt"},
    "pt-BR-FranciscaNeural": {"name": "Francisca", "engine": "online", "gender": "f", "lang": "pt"},
    # Multilíngue: fala os dois idiomas, então nunca é trocada por causa do idioma do texto
    "pt-BR-ThalitaMultilingualNeural": {"name": "Thalita", "engine": "online", "gender": "f", "lang": "pt",
                                        "multilingual": True},
    "en-US-AndrewNeural": {"name": "Andrew", "engine": "online", "gender": "m", "lang": "en"},
    "en-US-AvaNeural": {"name": "Ava", "engine": "online", "gender": "f", "lang": "en"},
    "pf_dora": {"name": "Dora", "engine": "offline", "gender": "f", "lang": "pt"},
    "pm_alex": {"name": "Alex", "engine": "offline", "gender": "m", "lang": "pt"},
    "af_heart": {"name": "Heart", "engine": "offline", "gender": "f", "lang": "en"},
    "am_michael": {"name": "Michael", "engine": "offline", "gender": "m", "lang": "en"},
}
DEFAULT_VOICE = "pt-BR-AntonioNeural"
KOKORO_LANG = {"pt": "pt-br", "en": "en-us"}

KOKORO_DIR = MODELS_DIR / "kokoro"
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_FILES = {"kokoro-v1.0.onnx": 325_532_387, "voices-v1.0.bin": 28_214_398}
OFFLINE_SIZE = "350 MB"

SPEECH_DIR = db.DATA_DIR / "speech"
PARTS_DIR = SPEECH_DIR / "parts"  # partes já geradas de um texto longo (apagadas ao juntar)
KEEP_DAYS = 30  # áudios gerados ficam guardados (para não gerar de novo) por este tempo

MAX_CHARS = 100_000  # ~2 horas de áudio
CHUNK_CHARS = 1500   # tamanho de cada parte (~1,5 min de fala)
ONLINE_PARALLEL = 4  # partes geradas ao mesmo tempo na voz online (medido: 4 rendem ~3,5×)
ONLINE_RETRIES = 3
PAUSE = 0.2          # segundos de silêncio entre as partes

OPUS_RATE = 48000


class SpeechError(Exception):
    status = 503  # o serviço de voz não está disponível


class InvalidText(SpeechError):
    status = 400


# ------------------------------------------------------------------ texto

MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
          "setembro", "outubro", "novembro", "dezembro"]


def _money(m: re.Match) -> str:
    reais = int(m["int"].replace(".", ""))
    cents = int(m["cents"] or 0)
    words = f"{reais} {'real' if reais == 1 else 'reais'}" if reais or not cents else ""
    if cents:
        words += f"{' e ' if words else ''}{cents} {'centavo' if cents == 1 else 'centavos'}"
    return words


def _date(m: re.Match) -> str:
    day, month = int(m["d"]), int(m["m"])
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return m[0]
    year = m["y"] and (f"20{m['y']}" if len(m["y"]) == 2 else m["y"])
    return f"{day} de {MONTHS[month - 1]}" + (f" de {year}" if year else "")


def _time(m: re.Match) -> str:
    hours = int(m["h"])
    words = f"{hours} {'hora' if hours == 1 else 'horas'}"
    return words + (f" e {int(m['min'])}" if m["min"] and int(m["min"]) else "")


def normalize(text: str, lang: str = "pt") -> str:
    """Escreve por extenso o que a voz costuma ler errado: valores, datas, horas, telefones e "3x".

    Só em português: as vozes em inglês já leem bem "$1,250.50", "10/03" e "2:30 PM"."""
    text = " ".join(text.split())
    if lang != "pt":
        return text
    text = re.sub(r"R\$\s?(?P<int>\d{1,3}(?:\.\d{3})+|\d+)(?:,(?P<cents>\d{2}))?", _money, text)
    text = re.sub(r"\b(?P<d>\d{1,2})/(?P<m>\d{1,2})(?:/(?P<y>\d{4}|\d{2}))?\b", _date, text)
    text = re.sub(r"\b(?P<h>[01]?\d|2[0-3]):(?P<min>[0-5]\d)(?:\s?h\b)?", _time, text)  # 14:30 ou 14:30h
    text = re.sub(r"\b(?P<h>[01]?\d|2[0-3])\s?h(?P<min>[0-5]\d)?\b", _time, text)
    text = re.sub(r"\b(\d+)\s?x\b", r"\1 vezes", text)
    # Telefone: "98765-4321" seria lido como "noventa e oito mil… menos…"; lê de dois em dois
    text = re.sub(r"\b(\d{4,5})-(\d{4})\b", lambda m: f"{_pairs(m[1])}, {_pairs(m[2])}", text)
    return text


def _pairs(digits: str) -> str:
    head = digits[: len(digits) % 2]
    return " ".join(([head] if head else []) + [digits[i:i + 2] for i in range(len(head), len(digits), 2)])


# Palavras comuns que só existem num dos idiomas (ficam de fora "a", "do", "no", "as", "me"…)
WORDS = {
    "en": frozenset("""the and is are was were you your that this with for have has had not what when will would
        can could should from they them their there here about which who how why of to in on at be been it its
        we he she his her my our but or if just than then these those very also into i i'm it's don't doesn't
        can't i'll we're you're that's hello hi good morning thanks thank please""".split()),
    "pt": frozenset("""o os um uma de da das dos em na nas nos para por com que não nao é e eu você voce vocês
        ele ela nós eles elas se mas ou como mais muito também tambem já ja foi são sao está esta estão tem
        tenho isso essa esse este aqui ali quando onde porque pra pro ao aos às à meu minha seu sua te lhe
        bom boa dia tarde noite obrigado obrigada olá ola oi tudo""".split()),
}
ACCENTS = re.compile(r"[ãõçáéíóúâêôà]")


def detect_language(text: str) -> str | None:
    """"pt" ou "en", pelo que predomina no começo do texto. None quando não dá para saber
    (texto curto demais, só números, os dois idiomas misturados)."""
    words = re.findall(r"[a-zà-ÿ']+", text[:5000].lower().replace("’", "'"))
    en = sum(w in WORDS["en"] for w in words)
    pt = sum(w in WORDS["pt"] or bool(ACCENTS.search(w)) for w in words)
    lang, hits, other = ("en", en, pt) if en > pt else ("pt", pt, en)
    if hits >= 2 and hits >= 2 * other:
        return lang
    if hits == 1 and not other and len(words) <= 4:  # "Thank you!", "Bom trabalho"
        return lang
    return None


def voice_for(lang: str, engine: str, gender: str) -> str:
    """A voz de um idioma, do mesmo tipo (online/offline) e gênero."""
    return next(id_ for id_, v in VOICES.items() if (v["lang"], v["engine"], v["gender"]) == (lang, engine, gender))


def offline_installed() -> bool:
    return all((KOKORO_DIR / name).is_file() for name in KOKORO_FILES)


def voices() -> list[dict]:
    installed = offline_installed()
    return [
        {"id": id_, **v, "available": installed if v["engine"] == "offline" else ONLINE}
        for id_, v in VOICES.items()
    ]


# ------------------------------------------------------------------ vozes online

async def _online(text: str, voice: str, speed: float) -> bytes:
    import edge_tts

    rate = f"{round((speed - 1) * 100):+d}%"
    for attempt in range(ONLINE_RETRIES):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate, connect_timeout=8, receive_timeout=30)
            mp3 = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3.extend(chunk["data"])
            if not mp3:
                raise SpeechError("O serviço de voz não devolveu áudio")
            return bytes(mp3)
        except Exception:
            if attempt == ONLINE_RETRIES - 1:
                raise
            await asyncio.sleep(1 + attempt)  # falha passageira (muitos pedidos, conexão instável)


# ------------------------------------------------------------------ vozes offline (Kokoro)

_kokoro = None
_kokoro_lock = threading.Lock()


def _offline_sync(text: str, voice: str, speed: float) -> bytes:
    global _kokoro
    with _kokoro_lock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro

            names = list(KOKORO_FILES)
            _kokoro = Kokoro(str(KOKORO_DIR / names[0]), str(KOKORO_DIR / names[1]))
        lang = KOKORO_LANG[VOICES[voice]["lang"]]
        samples, sample_rate = _kokoro.create(text, voice=voice, speed=speed, lang=lang)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


async def _offline(text: str, voice: str, speed: float) -> bytes:
    if not offline_installed():
        raise SpeechError("A voz offline não está baixada. Baixe em Configurações → Voz.")
    return await anyio.to_thread.run_sync(_offline_sync, text, voice, speed)


async def download_offline() -> AsyncIterator[dict]:
    """Baixa a voz offline, informando o progresso ({completed, total})."""
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    total = sum(KOKORO_FILES.values())
    done = sum(size for name, size in KOKORO_FILES.items() if (KOKORO_DIR / name).is_file())
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, connect=10)) as client:
            for name in KOKORO_FILES:
                target = KOKORO_DIR / name
                if target.is_file():
                    continue
                part = target.with_suffix(target.suffix + ".part")
                async with client.stream("GET", f"{KOKORO_URL}/{name}") as r:
                    r.raise_for_status()
                    last = 0.0
                    with open(part, "wb") as out:
                        async for chunk in r.aiter_bytes(1024 * 1024):
                            out.write(chunk)
                            done += len(chunk)
                            if time.monotonic() - last > 0.25:
                                last = time.monotonic()
                                yield {"completed": done, "total": total}
                part.replace(target)
    except httpx.HTTPError as e:
        raise SpeechError(f"Não foi possível baixar a voz offline: {e}") from e
    yield {"completed": total, "total": total, "status": "success"}


# ------------------------------------------------------------------ geração

def split_text(text: str, size: int | None = None) -> list[str]:
    """Divide o texto em partes de até `size` (CHUNK_CHARS) caracteres, cortando no fim das frases
    (ou, numa frase longa demais, nas vírgulas e depois entre palavras)."""
    size = size or CHUNK_CHARS

    def pieces(sentence: str, separators: list[str]) -> list[str]:
        if len(sentence) <= size:
            return [sentence]
        if not separators:  # sem pontuação nenhuma: corta entre palavras
            return textwrap.wrap(sentence, size, break_long_words=True)
        parts = re.split(separators[0], sentence)
        return [p for part in parts for p in pieces(part, separators[1:])]

    chunks: list[str] = []
    for piece in pieces(text, [r"(?<=[.!?…])\s+", r"(?<=[,;:])\s+"]):
        if chunks and len(chunks[-1]) + 1 + len(piece) <= size:
            chunks[-1] += " " + piece
        else:
            chunks.append(piece)
    return [c for c in chunks if c.strip()]


def _key(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


def file_path(name: str) -> Path | None:
    """O arquivo de um áudio gerado, pelo nome que a API devolveu."""
    if not re.fullmatch(r"[0-9a-f]{32}\.ogg", name):
        return None
    path = SPEECH_DIR / name
    return path if path.is_file() else None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")  # dois pedidos iguais ao mesmo tempo não se atrapalham
    tmp.write_bytes(data)
    tmp.replace(path)


async def _render(chunks: list[str], voice: str, speed: float) -> AsyncIterator[list[Path]]:
    """Gera as partes (várias ao mesmo tempo na voz online) e as guarda em PARTS_DIR, para que
    uma falha no meio não perca o que já foi feito. A cada parte pronta, devolve a lista
    das prontas; no fim, a lista completa, na ordem do texto."""
    online = VOICES[voice]["engine"] == "online"
    engine, ext, parallel = (_online, ".mp3", ONLINE_PARALLEL) if online else (_offline, ".wav", 1)
    paths = [PARTS_DIR / f"{_key(voice, f'{speed:.2f}', chunk)}{ext}" for chunk in chunks]
    limit = asyncio.Semaphore(parallel)

    async def part(chunk: str, path: Path) -> Path:
        if not path.exists():
            async with limit:
                _write(path, await engine(chunk, voice, speed))
        return path

    tasks = [asyncio.create_task(part(c, p)) for c, p in zip(chunks, paths)]
    try:
        done: list[Path] = []
        for task in asyncio.as_completed(tasks):
            done.append(await task)
            yield done
    finally:
        for task in tasks:
            task.cancel()
    yield paths


def _join(parts: list[Path], out: Path) -> float:
    """Junta as partes num .ogg Opus, com uma pausa curta entre elas. Devolve a duração em segundos."""
    pause = np.zeros((1, int(OPUS_RATE * PAUSE)), dtype=np.int16)
    samples = 0
    tmp = out.with_suffix(f".{uuid.uuid4().hex}.tmp")
    with av.open(str(tmp), "w", format="ogg") as dst:
        stream = dst.add_stream("libopus", rate=OPUS_RATE, layout="mono")
        stream.bit_rate = 32000

        def write(frame: av.AudioFrame) -> None:
            nonlocal samples
            frame.pts = samples
            samples += frame.samples
            dst.mux(stream.encode(frame))

        for i, part in enumerate(parts):
            if i:
                silence = av.AudioFrame.from_ndarray(pause, format="s16", layout="mono")
                silence.sample_rate = OPUS_RATE
                write(silence)
            resampler = av.AudioResampler(format="s16", layout="mono", rate=OPUS_RATE)
            with av.open(str(part)) as src:
                for frame in [*src.decode(audio=0), None]:
                    for resampled in resampler.resample(frame):
                        write(resampled)
        dst.mux(stream.encode(None))
    tmp.replace(out)
    return samples / OPUS_RATE


def prepare(text: str, voice: str) -> tuple[str, str, str]:
    """Confere o pedido antes de começar a gerar. Devolve o texto pronto para a voz, a voz que
    vai ler (a do idioma do texto, quando ele não é o da voz pedida) e o idioma."""
    if voice not in VOICES:
        raise InvalidText("Voz inválida")
    info = VOICES[voice]
    lang = detect_language(text) or info["lang"]
    if lang != info["lang"] and not info.get("multilingual"):
        voice = voice_for(lang, info["engine"], info["gender"])
    text = normalize(text, lang)
    if not text:
        raise InvalidText("Escreva um texto para gerar o áudio")
    if len(text) > MAX_CHARS:
        raise InvalidText(f"Texto muito longo (máximo de {MAX_CHARS:,} caracteres)".replace(",", "."))
    return text, voice, lang


async def synthesize(text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0) -> AsyncIterator[dict]:
    """Gera (ou reaproveita) o áudio de um texto de qualquer tamanho, informando o progresso:

    {"done": 3, "total": 8}                     a cada parte pronta
    {"fallback": "pm_alex"}                     a voz online falhou; recomeça com a offline
    {"joining": True}                           juntando as partes
    {"file": "<nome>.ogg", "voice", "duration"}  no fim ("voice" é a que leu: pode não ser a pedida)
    """
    text, voice, lang = prepare(text, voice)
    chunks = split_text(text)

    candidates = [voice]
    if VOICES[voice]["engine"] == "online":
        fallback = voice_for(lang, "offline", VOICES[voice]["gender"])
        if not ONLINE:
            if not offline_installed():
                raise SpeechError("As vozes online estão desligadas (ZAPSCRIBE_TTS_ONLINE=0). Baixe a voz offline.")
            candidates = [fallback]
        elif offline_installed():
            candidates.append(fallback)

    for attempt, current in enumerate(candidates):
        out = SPEECH_DIR / f"{_key(current, f'{speed:.2f}', text)}.ogg"
        if out.exists():
            out.touch()  # usado de novo: conta o prazo de KEEP_DAYS a partir de agora
            with av.open(str(out)) as f:
                duration = float(f.duration or 0) / av.time_base
            yield {"file": out.name, "voice": current, "duration": duration}
            return
        if attempt:
            yield {"fallback": current}
        try:
            yield {"done": 0, "total": len(chunks)}
            async for parts in _render(chunks, current, speed):
                if len(parts) < len(chunks):
                    yield {"done": len(parts), "total": len(chunks)}
            yield {"joining": True}
            duration = await anyio.to_thread.run_sync(_join, parts, out)
        except Exception as e:
            if VOICES[current]["engine"] == "offline":
                raise
            log.warning("Voz online falhou (%s: %s)", type(e).__name__, e)
            if current is candidates[-1]:
                raise SpeechError("Não foi possível usar a voz online (sem internet?). "
                                  "Baixe a voz offline em Configurações para gerar áudio sem internet.") from e
            continue
        for part in set(parts):
            part.unlink(missing_ok=True)
        yield {"done": len(chunks), "total": len(chunks)}
        yield {"file": out.name, "voice": current, "duration": duration}
        return


def cleanup() -> None:
    """Apaga os áudios gerados (e partes que sobraram de uma falha) há mais de KEEP_DAYS dias."""
    limit = time.time() - KEEP_DAYS * 86400
    for folder in (SPEECH_DIR, PARTS_DIR):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < limit:
                    path.unlink()
            except OSError:
                pass
