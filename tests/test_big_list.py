"""Histórico grande: a lista desenha aos poucos, mas busca, seleção e navegação continuam valendo para tudo."""

import json
import shutil
import sqlite3
import time

import pytest

from conftest import AUDIO

N = 400
PAGE = 150  # LIST_PAGE no app.js


@pytest.fixture(scope="module")
def seeded(server, api):
    """Coloca N áudios já transcritos direto no banco do servidor de teste."""
    shutil.copy(AUDIO, server["data"] / "audios" / "shared.ogg")
    now = time.time()
    with sqlite3.connect(server["data"] / "zapscribe.db") as conn:
        conn.executemany(
            "INSERT INTO transcriptions (title, original_name, file_name, size, duration, recorded_at, created_at, "
            "updated_at, status, progress, model, language, segments, text) "
            "VALUES (?, ?, 'shared.ogg', 1, 7, ?, ?, ?, 'done', 1, 'small', 'pt', ?, ?)",
            [
                (f"Áudio item{i:04d}", f"a{i}.ogg", now - i * 600, now, now,
                 json.dumps([{"start": 0, "end": 5, "text": f"Texto do item{i:04d}"}]), f"Texto do item{i:04d}")
                for i in range(N)
            ],
        )
    assert len(api.get("/api/transcriptions").json()) == N


def rows(page):
    return page.locator("#queue .item").count()


def test_desenha_so_o_comeco(page, server, seeded):
    page.goto(server["url"])
    page.wait_for_selector("#transcript .seg")
    assert rows(page) == PAGE
    assert page.inner_text("#tabs .tab[data-filter=all] .count") == str(N)


def test_rolar_carrega_mais(page):
    page.evaluate("el.queue.scrollTop = el.queue.scrollHeight")
    page.wait_for_function(f"document.querySelectorAll('#queue .item').length === {2 * PAGE}")
    page.evaluate("el.queue.scrollTop = el.queue.scrollHeight")
    page.wait_for_function(f"document.querySelectorAll('#queue .item').length === {N}")
    assert page.locator("#queue .list-more").count() == 0  # chegou ao fim


def test_busca_encontra_o_que_nao_estava_desenhado(page):
    page.fill("#search", "item0399")
    page.wait_for_function("document.querySelectorAll('#queue .item').length === 1")
    assert "item0399" in page.inner_text("#queue")
    page.fill("#search", "")
    page.wait_for_function(f"document.querySelectorAll('#queue .item').length === {PAGE}")  # voltou ao começo


def test_seta_passa_do_limite(page):
    order = page.evaluate("visibleItems().map(i => i.id)")
    page.evaluate(f"select({order[PAGE - 1]})")
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("ArrowDown")
    page.wait_for_function(f"state.selectedId === {order[PAGE]}")
    assert page.locator(f'#queue .item[data-id="{order[PAGE]}"].selected').count() == 1


def test_selecionar_todos_pega_a_lista_inteira(page):
    page.click("#checkAll")
    assert page.inner_text("#bulkCount") == f"{N} selecionados"
    page.keyboard.press("Escape")


def test_sem_erros_no_console(page):
    assert page.errors == []
