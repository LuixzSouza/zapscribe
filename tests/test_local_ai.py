"""IA local (Ollama), testada com um Ollama falso que responde igual ao de verdade."""

import time

import httpx
import pytest

from conftest import AUDIO, PTT, by_name, clipboard, free_port, row, upload, wait_status
from fake_ollama import FakeOllama


@pytest.fixture(scope="module")
def fake():
    with FakeOllama(free_port()) as f:
        yield f


@pytest.fixture(scope="module")
def server_env(fake):
    return {"OLLAMA_HOST": fake.url}


@pytest.fixture(scope="module")
def ptt(api):
    return wait_status(api, upload(api, PTT)["id"])


def generate(api, **body):
    with api.stream("POST", "/api/ai/local/generate", json=body) as r:
        chunks = list(r.iter_text()) if r.status_code == 200 else []
        return r, chunks, r.read() if r.status_code != 200 else None


# ------------------------------------------------------------ API

def test_status_lista_so_modelos_de_conversa(api, fake):
    s = api.get("/api/ai/local").json()
    assert s["available"] and [m["name"] for m in s["models"]] == ["llama3:latest"]  # sem o de embeddings
    assert s["model"] == "llama3:latest" and s["recommended"] == "qwen3:4b"


def test_gera_resposta_em_partes_e_guarda(api, fake, ptt):
    r, chunks, _ = generate(api, prompt="Resuma: " + ptt["text"], id=ptt["id"])
    assert r.status_code == 200 and r.headers["x-model"] == "llama3:latest"
    assert len(chunks) > 1  # chega aos poucos, não tudo no fim
    text = "".join(chunks)
    assert text.startswith("Resumo:") and fake.prompts[-1] == "Resuma: " + ptt["text"]
    full = api.get(f"/api/transcriptions/{ptt['id']}").json()
    assert full["ai_result"] == text.strip() and full["ai_model"] == "llama3:latest"


def test_sem_id_nao_guarda(api, ptt):
    before = api.get(f"/api/transcriptions/{ptt['id']}").json()["ai_result"]
    r, chunks, _ = generate(api, prompt="outro pedido")
    assert r.status_code == 200 and chunks
    assert api.get(f"/api/transcriptions/{ptt['id']}").json()["ai_result"] == before


def test_modelo_nao_instalado(api):
    r, _, body = generate(api, prompt="oi", model="nao-existe:1b")
    assert r.status_code == 502 and "não está instalado" in r.json()["detail"]


def test_id_inexistente(api):
    r, _, _ = generate(api, prompt="oi", id=9999)
    assert r.status_code == 404


def test_modelo_escolhido_nos_ajustes(api, fake):
    fake.models.append("gemma3:4b")
    api.put("/api/settings", json={"local_model": "gemma3:4b"})
    assert api.get("/api/ai/local").json()["model"] == "gemma3:4b"
    assert generate(api, prompt="oi")[0].headers["x-model"] == "gemma3:4b"
    api.put("/api/settings", json={"local_model": "foi-removido:1b"})  # não instalado: usa o menor
    assert api.get("/api/ai/local").json()["model"] == "llama3:latest"
    api.put("/api/settings", json={"local_model": ""})
    fake.models.remove("gemma3:4b")


def test_parar_no_meio_nao_guarda(api, fake, ptt):
    api.patch(f"/api/transcriptions/{ptt['id']}", json={"notes": "x"})
    before = api.get(f"/api/transcriptions/{ptt['id']}").json()["ai_result"]
    fake.slow = True
    cancelled = fake.cancelled
    try:
        with api.stream("POST", "/api/ai/local/generate", json={"prompt": "longo", "id": ptt["id"]}) as r:
            next(r.iter_text())  # recebe a primeira parte e desiste
    finally:
        fake.slow = False
    deadline = time.time() + 5
    while fake.cancelled == cancelled and time.time() < deadline:
        time.sleep(0.1)
    assert fake.cancelled > cancelled  # a geração no Ollama foi interrompida
    assert api.get(f"/api/transcriptions/{ptt['id']}").json()["ai_result"] == before


def test_baixar_modelo(api, fake):
    with api.stream("POST", "/api/ai/local/pull", json={"model": "qwen3:4b"}) as r:
        lines = [line for line in r.iter_lines() if line]
    assert r.status_code == 200 and '"total": 100' in lines[2] and '"success"' in lines[-1]
    assert "qwen3:4b" in [m["name"] for m in api.get("/api/ai/local").json()["models"]]
    fake.models.remove("qwen3:4b")


def test_sem_nenhum_modelo(api, fake):
    saved, fake.models[:] = list(fake.models), []
    try:
        assert api.get("/api/ai/local").json()["model"] is None
        assert generate(api, prompt="oi")[0].status_code == 409
    finally:
        fake.models[:] = saved


# ------------------------------------------------------------ interface

def test_ui_escolher_ia_local_e_baixar_modelo(page, api, ptt):
    page.wait_for_selector("#queue .item")
    page.click("#openSettings")
    page.wait_for_function("document.querySelector('#localModel').options.length > 0")
    assert "llama3:latest" in page.inner_text("#localModel")
    assert "Baixar qwen3:4b (2,5 GB)" in page.inner_text("#pullModel")
    page.locator("#aiPicker button[data-ai=local]").click()
    page.click("#pullModel")
    page.wait_for_function("document.querySelector('#toast').innerText.includes('instalado')")
    page.wait_for_function("document.querySelector('#localModel').value === 'qwen3:4b'")
    assert page.is_hidden("#pullModel")
    settings = api.get("/api/settings").json()
    assert settings["ai"] == "local" and settings["local_model"] == "qwen3:4b"
    page.keyboard.press("Escape")


def test_ui_resposta_aparece_e_fica_salva(page, api, ptt, server):
    row(page, "Oi, tudo bem").click()
    page.wait_for_selector("#transcript .seg")
    page.click("#vAi")
    page.wait_for_selector("#aiModal[open]")
    page.wait_for_function("document.querySelector('#aiState').innerText.startsWith('Pronto')")
    assert "qwen3:4b" in page.inner_text("#aiState")
    output = page.inner_text("#aiOutput")
    assert output.startswith("Resumo:")
    page.click("#aiCopy")
    page.wait_for_timeout(200)
    assert clipboard(page) == output.strip()
    page.click("#aiModal [data-close]")
    assert page.is_visible("#aiSaved") and page.inner_text("#aiSavedText") == output.strip()
    # Continua lá depois de recarregar a página
    page.goto(server["url"])
    row(page, "Oi, tudo bem").click()
    page.wait_for_selector("#aiSaved:not([hidden])")
    assert "qwen3:4b" in page.inner_text("#aiSavedInfo")


def test_ui_parar(page, fake):
    fake.slow = True
    try:
        page.click("#vAi")
        page.wait_for_function("document.querySelector('#aiState').innerText.startsWith('Escrevendo')")
        page.click("#aiStop")
        page.wait_for_function("document.querySelector('#aiState').innerText === 'Interrompido'")
        assert page.is_visible("#aiRetry") and page.is_hidden("#aiStop")
    finally:
        fake.slow = False
    page.click("#aiRetry")
    page.wait_for_function("document.querySelector('#aiState').innerText.startsWith('Pronto')")
    page.keyboard.press("Escape")


def test_ui_varios_audios_juntos(page, api):
    wait_status(api, upload(api, AUDIO)["id"])
    page.wait_for_function("document.querySelectorAll('#queue .item[data-state=done]').length === 2")
    page.click("#checkAll")
    page.click("#bulkAi")
    page.wait_for_function("document.querySelector('#aiState').innerText.startsWith('Pronto')")
    assert page.inner_text("#aiTitle") == "2 áudios"
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")


def test_ui_erro_aparece_na_janela(page, fake):
    saved, fake.models[:] = list(fake.models), []
    try:
        page.click("#vAi")
        page.wait_for_selector("#aiState.error")
        assert "Nenhum modelo" in page.inner_text("#aiState")
    finally:
        fake.models[:] = saved
    page.keyboard.press("Escape")


def test_ui_sem_erros_no_console(page):
    # O erro 409 do teste anterior é esperado; qualquer outro não
    assert [e for e in page.errors if "409" not in e] == []
