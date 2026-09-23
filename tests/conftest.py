"""Sobe um Zapscribe de verdade, com banco e pasta de áudios temporários, para cada arquivo de teste.

Os modelos já baixados em models/ são reaproveitados. Nada em data/ é alterado.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))  # permite importar os módulos do app nos testes
AUDIOS = Path(__file__).parent / "audios"
PTT = AUDIOS / "WhatsApp Ptt 2026-09-20 at 14.35.12.ogg"      # "Oi, tudo bem? Aqui é o Carlos. Preciso do orçamento…"
AUDIO = AUDIOS / "WhatsApp Audio 2026-09-21 at 09.05.33.ogg"  # "Bom dia. Queria confirmar a reunião de amanhã…"
VOCAB = AUDIOS / "vocabulario.ogg"                              # "Oi, aqui é a Thaynara. O Kauê pediu… Portobello…"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_env():
    """Variáveis extras para o servidor; um arquivo de teste pode sobrescrever esta fixture."""
    return {}


@pytest.fixture(scope="module")
def server(tmp_path_factory, server_env):
    port = free_port()
    data = tmp_path_factory.mktemp("data")
    env = {**os.environ, "ZAPSCRIBE_DATA_DIR": str(data), "ZAPSCRIBE_PORT": str(port), "PYTHONIOENCODING": "utf-8",
           "ZAPSCRIBE_WATCH_INTERVAL": "0.5", "OLLAMA_HOST": f"http://127.0.0.1:{free_port()}", **server_env}
    log = open(data / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "main.py", "--no-browser"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            if proc.poll() is not None:
                raise RuntimeError(f"O servidor não subiu:\n{(data / 'server.log').read_text(encoding='utf-8')}")
            try:
                httpx.get(f"{url}/api/config", timeout=1)
                break
            except httpx.TransportError:
                time.sleep(0.2)
        yield {"url": url, "data": data}
    finally:
        proc.terminate()
        proc.wait(10)
        log.close()


@pytest.fixture(scope="module")
def api(server):
    with httpx.Client(base_url=server["url"], timeout=60) as client:
        yield client


def upload(api, path: Path, **data) -> dict:
    return upload_bytes(api, path.name, path.read_bytes(), **data)


def upload_bytes(api, name: str, content: bytes, **data) -> dict:
    r = api.post("/api/transcriptions", files={"file": (name, content)}, data={"model": "small", **data})
    assert r.status_code in (200, 201), r.text
    return r.json()


def wait_status(api, id_: int, want=("done", "error"), timeout=300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = api.get(f"/api/transcriptions/{id_}").json()
        if item["status"] in want:
            return item
        time.sleep(0.3)
    raise TimeoutError(f"Transcrição {id_} ficou em '{item['status']}'")


# ------------------------------------------------------------ interface

@pytest.fixture(scope="module")
def page(server):
    """Navegador sem janela na página do app (Edge, Chrome ou o Chromium do Playwright)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        browser = None
        for opts in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                browser = p.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"], **opts)
                break
            except Exception:
                continue
        if not browser:
            pytest.skip("Nenhum navegador disponível (instale com: uv run playwright install chromium)")
        ctx = browser.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True,
                                  permissions=["clipboard-read", "clipboard-write"])
        pg = ctx.new_page()
        pg.errors = []
        pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        # Registra os links abertos em vez de abrir o ChatGPT/Claude de verdade
        pg.add_init_script("window.__opened = []; window.open = (u) => { window.__opened.push(u); return null; };")
        pg.goto(server["url"])
        yield pg
        browser.close()


def by_name(api, name):
    return next(i for i in api.get("/api/transcriptions").json() if i["original_name"] == name)


def clipboard(page):
    return page.evaluate("navigator.clipboard.readText()")


def row(page, text):
    return page.locator("#queue .item", has_text=text).first
