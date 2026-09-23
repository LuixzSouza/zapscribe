"""Importação automática dos áudios do WhatsApp salvos numa pasta."""

import os
import shutil
import time

import pytest

from conftest import AUDIO, PTT, VOCAB, wait_status

# O servidor espera o arquivo ficar 2 s sem mudar; a pasta é verificada a cada 0,5 s nos testes
WAIT = 6


@pytest.fixture(scope="module")
def folder(tmp_path_factory):
    return tmp_path_factory.mktemp("Downloads")


def put(folder, src, name, age=0):
    """Copia um áudio para a pasta, como se tivesse sido salvo há `age` segundos."""
    dest = folder / name
    shutil.copy(src, dest)
    if age:
        t = time.time() - age
        os.utime(dest, (t, t))
    return dest


def names(api):
    return sorted(i["original_name"] for i in api.get("/api/transcriptions").json())


def wait_for(api, name, timeout=WAIT + 4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if name in names(api):
            return next(i for i in api.get("/api/transcriptions").json() if i["original_name"] == name)
        time.sleep(0.3)
    raise AssertionError(f"{name} não foi importado. Lista: {names(api)}")


def test_desligado_por_padrao(api, folder):
    s = api.get("/api/settings").json()
    assert s["watch_enabled"] is False and s["watch_folder"] == ""
    assert api.get("/api/config").json()["downloads"].endswith("Downloads")


def test_ignora_o_que_ja_estava_na_pasta(api, folder):
    put(folder, AUDIO, "WhatsApp Audio 2026-09-01 at 10.00.00.ogg", age=120)
    s = api.put("/api/settings", json={"watch_enabled": True, "watch_folder": str(folder), "watch_since": 1}).json()
    assert s["watch_enabled"] and s["watch_since"] > time.time() - 10  # definido pelo servidor, não pelo pedido
    time.sleep(WAIT)
    assert names(api) == []


def test_importa_audio_novo(api, folder):
    put(folder, PTT, PTT.name)
    item = wait_status(api, wait_for(api, PTT.name)["id"])
    assert item["status"] == "done" and "orçamento" in item["text"].lower()
    assert abs(item["recorded_at"] - time.mktime((2026, 9, 20, 14, 35, 12, 0, 0, -1))) < 1


def test_ignora_outros_arquivos(api, folder):
    put(folder, VOCAB, "recado.ogg")
    put(folder, VOCAB, "WhatsApp Ptt 2026-09-22 at 08.00.00.ogg.crdownload")
    (folder / "WhatsApp Image 2026-09-22 at 08.00.00.jpeg").write_bytes(b"jpg")
    time.sleep(WAIT)
    assert names(api) == [PTT.name]


def test_mesmo_audio_com_outro_nome_nao_duplica(api, folder):
    put(folder, PTT, "WhatsApp Ptt 2026-09-20 at 14.35.12 (1).ogg")
    time.sleep(WAIT)
    assert names(api) == [PTT.name]


def test_excluido_nao_volta(api, folder):
    id_ = wait_for(api, PTT.name)["id"]
    api.delete(f"/api/transcriptions/{id_}")
    time.sleep(WAIT)
    assert names(api) == []


def test_varios_de_uma_vez(api, folder):
    put(folder, AUDIO, AUDIO.name)
    put(folder, VOCAB, "WhatsApp Audio 2026-09-22 at 11.11.11.ogg")
    wait_for(api, AUDIO.name)
    wait_for(api, "WhatsApp Audio 2026-09-22 at 11.11.11.ogg")


def test_desligar_para_de_importar(api, folder):
    api.put("/api/settings", json={"watch_enabled": False})
    before = names(api)
    put(folder, PTT, "WhatsApp Ptt 2026-09-23 at 09.00.00.ogg")
    time.sleep(WAIT)
    assert names(api) == before


def test_pasta_que_nao_existe_nao_quebra(api, folder, server):
    api.put("/api/settings", json={"watch_enabled": True, "watch_folder": str(folder / "nao-existe")})
    time.sleep(2)
    assert api.get("/api/config").status_code == 200
    api.put("/api/settings", json={"watch_enabled": False, "watch_folder": ""})
    assert "Falha ao verificar" not in (server["data"] / "server.log").read_text(encoding="utf-8")
