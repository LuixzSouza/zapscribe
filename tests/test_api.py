"""Testes da API com transcrições reais (modelo small). Os testes rodam em ordem e compartilham o servidor."""

import json
import sqlite3
import threading
import time
import zipfile

import httpx
import pytest

from conftest import AUDIO, PTT, VOCAB, upload, upload_bytes, wait_status


@pytest.fixture(scope="module")
def carlos(api):
    return api.post("/api/clients", json={"name": "  Carlos Reforma ", "phone": "11 99999-0000"}).json()


@pytest.fixture(scope="module")
def ptt(api, carlos):
    item = upload(api, PTT, client_id=str(carlos["id"]), language="pt")
    return wait_status(api, item["id"])


class Events:
    """Escuta /api/events em segundo plano."""

    def __init__(self, url):
        self.items = []
        threading.Thread(target=self._run, args=(url,), daemon=True).start()
        time.sleep(0.5)

    def _run(self, url):
        try:
            with httpx.stream("GET", f"{url}/api/events", timeout=None) as r:
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        self.items.append(json.loads(line[6:]))
        except httpx.TransportError:
            pass  # servidor desligado no fim dos testes

    def wait(self, pred, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(pred(e) for e in self.items):
                return True
            time.sleep(0.1)
        return False


def test_pagina_e_arquivos(api):
    assert "<html" in api.get("/").text.lower()
    for f in ["app.js", "style.css", "favicon.svg", "vendor/icons.svg"]:
        assert api.get(f"/static/{f}").status_code == 200


def test_config(api):
    cfg = api.get("/api/config").json()
    assert cfg["default"] == "small" and "large-v3-turbo" in cfg["models"]
    assert cfg["device"] in ("cpu", "cuda")


def test_bloqueia_outros_sites(api):
    # DNS rebinding: domínio de fora apontando para 127.0.0.1
    assert api.get("/api/transcriptions", headers={"host": "evil.example:8000"}).status_code == 400
    # CSRF: outro site enviando dados
    r = api.post("/api/clients", json={"name": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = api.post("/api/clients", json={"name": "x"}, headers={"origin": "null"})
    assert r.status_code == 403
    assert not any(c["name"] == "x" for c in api.get("/api/clients").json())
    # A própria página continua funcionando
    r = api.post("/api/clients", json={"name": "Local"}, headers={"origin": str(api.base_url).rstrip("/")})
    assert r.status_code == 201
    api.delete(f"/api/clients/{r.json()['id']}")
    assert api.get("/api/config", headers={"host": "localhost"}).status_code == 200


def test_clientes(api, carlos):
    assert carlos["name"] == "Carlos Reforma" and carlos["count"] == 0 and carlos["vocabulary"] == ""
    assert api.post("/api/clients", json={"name": ""}).status_code == 422
    c = api.patch(f"/api/clients/{carlos['id']}", json={"notes": "obra"}).json()
    assert c["notes"] == "obra" and c["phone"] == "11 99999-0000"
    assert api.patch("/api/clients/9999", json={"notes": "x"}).status_code == 404


def test_validacoes_de_envio(api):
    files = {"file": (PTT.name, PTT.read_bytes())}
    assert api.post("/api/transcriptions", files=files, data={"model": "xpto"}).status_code == 400
    assert api.post("/api/transcriptions", files=files, data={"client_id": "9999"}).status_code == 404


def test_transcreve_audio_do_whatsapp(api, ptt, carlos):
    assert ptt["status"] == "done", ptt["error"]
    assert "orçamento" in ptt["text"].lower()
    assert ptt["segments"] and ptt["duration"] > 3 and ptt["detected_language"] == "pt"
    assert ptt["title_auto"] and ptt["title"].startswith("Oi, tudo bem")
    assert abs(ptt["recorded_at"] - time.mktime((2026, 9, 20, 14, 35, 12, 0, 0, -1))) < 1
    assert "file_name" not in ptt
    assert next(c for c in api.get("/api/clients").json() if c["id"] == carlos["id"])["count"] == 1


def test_mesmo_audio_nao_duplica(api, server, ptt):
    again = upload(api, PTT)
    assert again["duplicate"] is True and again["id"] == ptt["id"]
    assert len(list((server["data"] / "audios").iterdir())) == 1


def test_vocabulario_do_cliente(api):
    obra = api.post("/api/clients", json={"name": "Obra", "vocabulary": "Thaynara, Kauê, porcelanato Portobello"}).json()
    item = wait_status(api, upload(api, VOCAB, client_id=str(obra["id"]), last_modified="1758400000000")["id"])
    assert "Thaynara" in item["text"] and "Portobello" in item["text"], item["text"]
    assert abs(item["recorded_at"] - 1758400000) < 1  # sem data no nome: usa a do arquivo
    api.delete(f"/api/transcriptions/{item['id']}")
    api.delete(f"/api/clients/{obra['id']}")


def test_vocabulario_geral_vale_ao_refazer(api):
    item = wait_status(api, upload(api, VOCAB)["id"])
    api.put("/api/settings", json={"vocabulary": "Thaynara\nPortobello"})
    api.post(f"/api/transcriptions/{item['id']}/retranscribe", json={"model": "small"})
    item = wait_status(api, item["id"])
    assert "Thaynara" in item["text"] and "Portobello" in item["text"], item["text"]
    api.put("/api/settings", json={"vocabulary": ""})
    api.delete(f"/api/transcriptions/{item['id']}")


def test_eventos_em_tempo_real(api, server, ptt):
    events = Events(server["url"])
    api.patch(f"/api/transcriptions/{ptt['id']}", json={"notes": "evento"})
    assert events.wait(lambda e: e["type"] == "item" and e["item"]["notes"] == "evento")
    item = upload(api, AUDIO)
    assert events.wait(lambda e: e["type"] == "segment" and e["id"] == item["id"])
    wait_status(api, item["id"])
    api.delete(f"/api/transcriptions/{item['id']}")
    assert events.wait(lambda e: e["type"] == "deleted" and item["id"] in e["ids"])


def test_editar(api, ptt):
    id_ = ptt["id"]
    j = api.patch(f"/api/transcriptions/{id_}", json={"title": "Orçamento cozinha", "notes": "ligar", "resolved": True}).json()
    assert j["title"] == "Orçamento cozinha" and j["notes"] == "ligar" and j["resolved"] and not j["title_auto"]
    segs = ptt["segments"]
    segs[0]["text"] = "TEXTO EDITADO"
    assert api.patch(f"/api/transcriptions/{id_}", json={"segments": segs}).json()["text"].startswith("TEXTO EDITADO")
    assert api.patch(f"/api/transcriptions/{id_}", json={"title": ""}).status_code == 422
    assert api.patch(f"/api/transcriptions/{id_}", json={"client_id": 9999}).status_code == 404
    assert api.patch("/api/transcriptions/9999", json={"notes": "x"}).status_code == 404


def test_audio_original(api, ptt):
    r = api.get(f"/api/transcriptions/{ptt['id']}/audio")
    assert r.status_code == 200 and r.content == PTT.read_bytes() and "ogg" in r.headers["content-type"]
    r = api.get(f"/api/transcriptions/{ptt['id']}/audio", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100  # permite pular no player


def test_refazer_com_outro_modelo(api, ptt):
    id_ = ptt["id"]
    r = api.post(f"/api/transcriptions/{id_}/retranscribe", json={"model": "medium"})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert api.post(f"/api/transcriptions/{id_}/retranscribe", json={"model": "medium"}).status_code == 409
    assert api.post(f"/api/transcriptions/{id_}/retranscribe", json={"model": "x"}).status_code == 400
    item = wait_status(api, id_)
    assert item["status"] == "done" and item["model"] == "medium" and "orçamento" in item["text"].lower()
    assert item["title"] == "Orçamento cozinha"  # título editado à mão é mantido


def test_acoes_em_lote(api, ptt, carlos):
    other = wait_status(api, upload(api, AUDIO)["id"])
    ids = [ptt["id"], other["id"]]
    listed = lambda: [i for i in api.get("/api/transcriptions").json() if i["id"] in ids]
    api.post("/api/transcriptions/bulk", json={"ids": ids, "action": "resolve"})
    assert all(i["resolved"] for i in listed())
    api.post("/api/transcriptions/bulk", json={"ids": ids, "action": "unresolve"})
    assert not any(i["resolved"] for i in listed())
    api.post("/api/transcriptions/bulk", json={"ids": ids, "action": "set_client", "client_id": carlos["id"]})
    assert all(i["client_id"] == carlos["id"] for i in listed())
    assert api.post("/api/transcriptions/bulk", json={"ids": [], "action": "resolve"}).status_code == 422
    assert api.post("/api/transcriptions/bulk", json={"ids": ids, "action": "x"}).status_code == 422


def test_excluir_cliente_mantem_audios(api):
    tmp = api.post("/api/clients", json={"name": "Apagar"}).json()
    ids = [i["id"] for i in api.get("/api/transcriptions").json()]
    api.post("/api/transcriptions/bulk", json={"ids": ids, "action": "set_client", "client_id": tmp["id"]})
    assert api.delete(f"/api/clients/{tmp['id']}").status_code == 204
    assert api.delete(f"/api/clients/{tmp['id']}").status_code == 404
    items = api.get("/api/transcriptions").json()
    assert len(items) == len(ids) and all(i["client_id"] is None for i in items)


def test_prompts(api):
    ts = api.get("/api/templates").json()
    assert len(ts) == 4
    t = api.post("/api/templates", json={"name": "Teste", "body": "Oi {texto}"}).json()
    assert t["position"] == 4
    assert api.patch(f"/api/templates/{t['id']}", json={"body": "X {cliente}"}).json()["body"] == "X {cliente}"
    assert api.patch(f"/api/templates/{t['id']}", json={"body": ""}).status_code == 422
    assert api.delete(f"/api/templates/{t['id']}").status_code == 204
    assert api.delete(f"/api/templates/{t['id']}").status_code == 404


def test_ajustes(api):
    tid = api.get("/api/templates").json()[1]["id"]
    s = api.put("/api/settings", json={"ai": "claude", "template_id": tid, "resolve_on_send": True, "vocabulary": "a, b", "lixo": 1}).json()
    assert "lixo" not in s  # chaves desconhecidas são ignoradas
    assert {k: s[k] for k in ("ai", "template_id", "resolve_on_send", "vocabulary")} == {
        "ai": "claude", "template_id": tid, "resolve_on_send": True, "vocabulary": "a, b"}
    assert api.get("/api/settings").json() == s
    api.put("/api/settings", json={"vocabulary": ""})


def test_arquivo_invalido(api):
    item = upload_bytes(api, "x.wav", b"isto nao e audio")
    item = wait_status(api, item["id"], timeout=60)
    assert item["status"] == "error"
    assert item["error"].startswith("Não foi possível ler este arquivo")
    api.delete(f"/api/transcriptions/{item['id']}")


def test_id_de_excluido_nao_e_reaproveitado(api):
    a = upload_bytes(api, "a.wav", b"um")
    api.delete(f"/api/transcriptions/{a['id']}")
    b = upload_bytes(api, "b.wav", b"dois")
    assert b["id"] > a["id"]
    wait_status(api, b["id"], timeout=60)
    api.delete(f"/api/transcriptions/{b['id']}")


def test_excluir_durante_a_transcricao(api, server, ptt):
    api.post(f"/api/transcriptions/{ptt['id']}/retranscribe", json={"model": "large-v3-turbo"})
    wait_status(api, ptt["id"], want=("running",), timeout=60)
    assert api.delete(f"/api/transcriptions/{ptt['id']}").status_code == 204
    assert api.get(f"/api/transcriptions/{ptt['id']}").status_code == 404
    assert api.delete(f"/api/transcriptions/{ptt['id']}").status_code == 404
    time.sleep(5)  # o trabalho em andamento termina sem recriar nada
    assert api.get(f"/api/transcriptions/{ptt['id']}").status_code == 404
    log = (server["data"] / "server.log").read_text(encoding="utf-8")
    assert f"Erro ao transcrever {ptt['id']}" not in log


def test_ia_local_com_ollama_fechado(api, ptt):
    s = api.get("/api/ai/local").json()
    assert s["available"] is False and s["models"] == [] and s["model"] is None
    r = api.post("/api/ai/local/generate", json={"prompt": "oi", "model": "llama3:latest"})
    assert r.status_code == 502 and "não está aberto" in r.json()["detail"]
    r = api.post("/api/ai/local/pull", json={"model": "qwen3:4b"})
    assert r.status_code == 502 and "não está aberto" in r.json()["detail"]


def test_backup(api, tmp_path):
    items = api.get("/api/transcriptions").json()
    r = api.get("/api/backup")
    assert r.status_code == 200 and "zapscribe-backup-" in r.headers["content-disposition"]
    (tmp_path / "b.zip").write_bytes(r.content)
    with zipfile.ZipFile(tmp_path / "b.zip") as z:
        z.extractall(tmp_path / "b")
        audios = [n for n in z.namelist() if n.startswith("audios/")]
    assert len(audios) == len(items)
    with sqlite3.connect(tmp_path / "b" / "zapscribe.db") as copy:
        assert copy.execute("SELECT COUNT(*) FROM transcriptions").fetchone()[0] == len(items)


def test_excluir_tudo_apaga_os_arquivos(api, server):
    ids = [i["id"] for i in api.get("/api/transcriptions").json()]
    assert api.post("/api/transcriptions/bulk", json={"ids": ids, "action": "delete"}).status_code == 200
    assert api.get("/api/transcriptions").json() == []
    assert list((server["data"] / "audios").iterdir()) == []
