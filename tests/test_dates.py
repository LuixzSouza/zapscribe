"""Data dos áudios a partir do nome do arquivo, inclusive quando o nome só traz o dia."""

import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from conftest import AUDIO, ROOT, by_name, free_port, upload_bytes, wait_status
from main import recorded_at_from

DAY_EVENING = time.mktime((2026, 9, 21, 18, 40, 0, 0, 0, -1))
DAYS_LATER = time.mktime((2026, 9, 30, 10, 0, 0, 0, 0, -1))
NOON = time.mktime((2026, 9, 21, 12, 0, 0, 0, 0, -1))


@pytest.mark.parametrize("name, modified, expected", [
    # Nome com dia e horário: vale o nome
    ("WhatsApp Audio 2026-09-21 at 09.05.33.ogg", DAYS_LATER, (time.mktime((2026, 9, 21, 9, 5, 33, 0, 0, -1)), False)),
    # Só o dia (Android ou WhatsApp sem horário): o horário do arquivo, se for do mesmo dia…
    ("PTT-20260921-WA0012.opus", DAY_EVENING, (DAY_EVENING, False)),
    # …senão só o dia (antes ficava com a data em que o arquivo foi salvo)
    ("PTT-20260921-WA0012.opus", DAYS_LATER, (NOON, True)),
    ("AUD-20260921-WA0003.opus", None, (NOON, True)),
    ("WhatsApp Audio 2026-09-21.ogg", DAYS_LATER, (NOON, True)),
    # Sem data no nome (ou data impossível): a do arquivo
    ("gravacao.mp3", DAYS_LATER, (DAYS_LATER, False)),
    ("PTT-20261341-WA0001.opus", DAYS_LATER, (DAYS_LATER, False)),
    ("gravacao.mp3", None, (None, False)),
])
def test_data_pelo_nome(name, modified, expected):
    assert recorded_at_from(name, modified * 1000 if modified else None) == expected


@pytest.mark.parametrize("name, imports", [
    ("WhatsApp Ptt 2026-09-20 at 14.35.12.ogg", True),
    ("PTT-20260921-WA0012.opus", True),
    ("AUD-20260921-WA0003 (1).opus", True),
    ("PTT-20260921-WA0012.opus.crdownload", False),
    ("IMG-20260921-WA0001.jpg", False),
    ("musica.mp3", False),
])
def test_importacao_automatica_reconhece_nomes_do_android(name, imports):
    import watcher
    assert bool(watcher.WHATSAPP_FILE.match(name)) is imports


def test_envio_com_so_o_dia(api):
    item = upload_bytes(api, "PTT-20260921-WA0012.opus", AUDIO.read_bytes(), last_modified=str(DAYS_LATER * 1000))
    assert item["date_only"] is True and item["recorded_at"] == NOON and item["title"] == "Áudio do WhatsApp"
    wait_status(api, item["id"])


def test_corrige_audios_antigos(tmp_path):
    """Áudios importados antes da correção ficavam com a data em que o arquivo foi salvo."""
    port = free_port()
    env = {"ZAPSCRIBE_DATA_DIR": str(tmp_path), "ZAPSCRIBE_PORT": str(port), "PYTHONIOENCODING": "utf-8"}

    def start():
        import os
        return subprocess.Popen([sys.executable, "main.py", "--no-browser"], cwd=ROOT, env={**os.environ, **env},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_up():
        for _ in range(150):
            try:
                return httpx.get(f"http://127.0.0.1:{port}/api/transcriptions", timeout=1).json()
            except httpx.TransportError:
                time.sleep(0.2)

    proc = start()
    try:
        wait_up()
    finally:
        proc.terminate()
        proc.wait(10)
    with sqlite3.connect(tmp_path / "zapscribe.db") as db:
        for name in ("PTT-20260921-WA0012.opus", "WhatsApp Ptt 2026-09-20 at 14.35.12.ogg"):
            db.execute("INSERT INTO transcriptions (title, original_name, file_name, size, recorded_at, created_at, "
                       "updated_at, status, model, language) VALUES (?, ?, 'x.ogg', 1, ?, 0, 0, 'done', 'small', 'pt')",
                       (name, name, DAYS_LATER if name.startswith("PTT") else time.mktime((2026, 9, 20, 14, 35, 12, 0, 0, -1))))
    proc = start()
    try:
        items = {i["original_name"]: i for i in wait_up()}
    finally:
        proc.terminate()
        proc.wait(10)
    assert items["PTT-20260921-WA0012.opus"]["recorded_at"] == NOON and items["PTT-20260921-WA0012.opus"]["date_only"]
    assert not items["WhatsApp Ptt 2026-09-20 at 14.35.12.ogg"]["date_only"]


def test_interface_mostra_so_o_dia(page, api):
    item = by_name(api, "PTT-20260921-WA0012.opus")
    page.reload()
    page.wait_for_selector("#queue .item")
    page.evaluate(f"select({item['id']})")
    page.wait_for_function("document.querySelector('#vDate').textContent === '21/09'")
    assert "só traz o dia" in page.get_attribute("#vDate", "title")
