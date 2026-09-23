"""IA local pelo Ollama (https://ollama.com): resume e sugere respostas sem mandar nada para a internet."""

import json
import os
from collections.abc import AsyncIterator

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = f"http://{OLLAMA_URL}"

# Sugerido quando não há nenhum modelo: leve, bom em português, roda bem na CPU
RECOMMENDED_MODEL = "qwen3:4b"
RECOMMENDED_SIZE = "2,5 GB"

# Famílias de modelos que só geram embeddings (não conversam)
EMBEDDING_FAMILIES = {"bert", "nomic-bert", "bge", "clip"}


class LocalAIError(Exception):
    pass


def _client(timeout: float | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=OLLAMA_URL, timeout=httpx.Timeout(timeout, connect=3))


def _error(data: bytes | str) -> str:
    try:
        return json.loads(data).get("error") or str(data)
    except (ValueError, AttributeError):
        return str(data)


async def status() -> dict:
    """Se o Ollama está aberto e quais modelos de conversa estão instalados (do menor ao maior)."""
    try:
        async with _client(5) as client:
            r = await client.get("/api/tags")
            r.raise_for_status()
    except httpx.HTTPError:
        return {"available": False, "models": [], "recommended": RECOMMENDED_MODEL, "recommended_size": RECOMMENDED_SIZE}
    models = [
        {"name": m["name"], "size": m.get("size", 0), "parameters": m.get("details", {}).get("parameter_size", "")}
        for m in r.json().get("models", [])
        if not set(m.get("details", {}).get("families") or [m.get("details", {}).get("family")]) & EMBEDDING_FAMILIES
    ]
    models.sort(key=lambda m: m["size"])
    return {"available": True, "models": models, "recommended": RECOMMENDED_MODEL, "recommended_size": RECOMMENDED_SIZE}


async def generate(prompt: str, model: str) -> AsyncIterator[str]:
    """Resposta em partes, conforme o modelo escreve. Cancelar o iterador interrompe a geração."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "think": False,  # modelos que "pensam" (ex.: qwen3) respondem direto, bem mais rápido
    }
    try:
        async with _client(None) as client, client.stream("POST", "/api/chat", json=body) as r:
            if r.status_code == 404:
                raise LocalAIError(f"O modelo “{model}” não está instalado no Ollama")
            if r.status_code != 200:
                raise LocalAIError(_error(await r.aread()))
            async for line in r.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise LocalAIError(data["error"])
                if chunk := data.get("message", {}).get("content"):
                    yield chunk
    except httpx.ConnectError as e:
        raise LocalAIError("O Ollama não está aberto. Abra o Ollama e tente de novo.") from e


async def pull(model: str) -> AsyncIterator[dict]:
    """Baixa um modelo, informando o progresso ({status, completed, total})."""
    try:
        async with _client(None) as client, client.stream("POST", "/api/pull", json={"model": model}) as r:
            if r.status_code != 200:
                raise LocalAIError(_error(await r.aread()))
            async for line in r.aiter_lines():
                if line:
                    data = json.loads(line)
                    if data.get("error"):
                        raise LocalAIError(data["error"])
                    yield data
    except httpx.ConnectError as e:
        raise LocalAIError("O Ollama não está aberto. Abra o Ollama e tente de novo.") from e
