"""Se a GPU for usada mas não funcionar (ex.: sem as bibliotecas CUDA), a transcrição continua na CPU."""

import os
import subprocess
import sys
import time

import httpx
import pytest

from conftest import AUDIO, ROOT, free_port, upload, wait_status


def test_gpu_que_falha_cai_para_a_cpu(tmp_path):
    import ctranslate2

    if ctranslate2.get_cuda_device_count() > 0:
        pytest.skip("Esta máquina tem GPU funcionando; o teste simula uma sem CUDA")

    port = free_port()
    env = {**os.environ, "ZAPSCRIBE_DATA_DIR": str(tmp_path), "ZAPSCRIBE_PORT": str(port), "ZAPSCRIBE_DEVICE": "cuda"}
    proc = subprocess.Popen([sys.executable, "main.py", "--no-browser"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    api = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)
    try:
        for _ in range(150):
            try:
                assert api.get("/api/config").json()["device"] == "cuda"
                break
            except httpx.TransportError:
                time.sleep(0.2)
        item = wait_status(api, upload(api, AUDIO)["id"])
        assert item["status"] == "done" and "reunião" in item["text"], item["error"]
        assert api.get("/api/config").json()["device"] == "cpu"
    finally:
        proc.terminate()
        proc.wait(10)
