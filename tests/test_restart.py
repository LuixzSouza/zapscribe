"""A fila continua depois que o servidor é desligado no meio das transcrições."""

import os
import subprocess
import sys
import time

import httpx

from conftest import AUDIO, PTT, ROOT, VOCAB, free_port, upload_bytes, wait_status


def start(data, port):
    env = {**os.environ, "ZAPSCRIBE_DATA_DIR": str(data), "ZAPSCRIBE_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, "main.py", "--no-browser"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/config", timeout=1)
            return proc
        except httpx.TransportError:
            time.sleep(0.2)
    raise RuntimeError("O servidor não subiu")


def test_fila_continua_depois_de_reiniciar(tmp_path):
    port = free_port()
    api = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)
    proc = start(tmp_path, port)
    try:
        # Conteúdos diferentes (o mesmo arquivo repetido seria reconhecido como duplicado)
        ids = [
            upload_bytes(api, f"{i}-{path.name}", path.read_bytes() + bytes(i), model="large-v3-turbo")["id"]
            for i, path in enumerate([PTT, AUDIO, VOCAB, PTT, AUDIO, VOCAB])
        ]
        wait_status(api, ids[0], want=("running",), timeout=60)
    finally:
        proc.kill()  # desligamento brusco, como fechar a janela
        proc.wait(10)

    proc = start(tmp_path, port)
    try:
        items = [wait_status(api, i, timeout=600) for i in ids]
        assert all(i["status"] == "done" and i["text"] for i in items), [(i["id"], i["status"], i["error"]) for i in items]
    finally:
        proc.terminate()
        proc.wait(10)
