"""Um Ollama de mentira, com a mesma API (/api/tags, /api/chat, /api/pull), para testar sem baixar modelos."""

import asyncio
import json
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class FakeOllama:
    def __init__(self, port: int, models=("llama3:latest",)):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.models = list(models)
        self.prompts: list[str] = []
        self.cancelled = 0
        self.slow = False  # responde devagar, para testar o botão "Parar"
        app = FastAPI()

        @app.get("/api/tags")
        def tags():
            return {"models": [
                {"name": m, "size": 1000 * (i + 1), "details": {"family": "llama", "parameter_size": "8B"}}
                for i, m in enumerate(self.models)
            ] + [{"name": "nomic-embed-text:latest", "size": 1, "details": {"family": "nomic-bert"}}]}

        @app.post("/api/chat")
        async def chat(request: Request):
            body = await request.json()
            if body["model"] not in self.models:
                return JSONResponse({"error": f"model '{body['model']}' not found"}, 404)
            prompt = body["messages"][-1]["content"]
            self.prompts.append(prompt)
            words = f"Resumo: o cliente pediu {len(prompt)} caracteres de coisas. Resposta sugerida: Olá!".split(" ")

            async def stream():
                try:
                    for i, word in enumerate(words):
                        await asyncio.sleep(0.5 if self.slow else 0.02)
                        yield json.dumps({"message": {"content": word + ("" if i == len(words) - 1 else " ")}, "done": False}) + "\n"
                    yield json.dumps({"message": {"content": ""}, "done": True}) + "\n"
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise

            return StreamingResponse(stream(), media_type="application/x-ndjson")

        @app.post("/api/pull")
        async def pull(request: Request):
            model = (await request.json())["model"]

            async def stream():
                yield json.dumps({"status": "pulling manifest"}) + "\n"
                for done in (0, 50, 100):
                    await asyncio.sleep(0.05)
                    yield json.dumps({"status": "downloading", "completed": done, "total": 100}) + "\n"
                self.models.append(model)
                yield json.dumps({"status": "success"}) + "\n"

            return StreamingResponse(stream(), media_type="application/x-ndjson")

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)
