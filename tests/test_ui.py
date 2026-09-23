"""Testes da interface num navegador de verdade (Edge ou Chromium, sem janela).

Os testes seguem o uso normal do app e rodam em ordem, na mesma página.
"""

import base64
from pathlib import Path

import pytest

from conftest import AUDIO, PTT, VOCAB, by_name, clipboard, row


def test_estado_vazio(page):
    page.wait_for_selector("#emptyState:not([hidden])")


def test_enviar_arquivos(page):
    page.set_input_files("#fileInput", [str(PTT), str(AUDIO), str(VOCAB)])
    page.wait_for_function("document.querySelectorAll('#queue .item').length === 3", timeout=20000)
    page.wait_for_function("document.querySelectorAll('#queue .item[data-state=done]').length === 3", timeout=180000)
    assert page.title() == "Zapscribe"
    assert len(page.locator("#queue .group-label").all_inner_texts()) >= 2  # agrupado por dia


def test_mesmo_arquivo_nao_duplica(page):
    page.set_input_files("#fileInput", [str(PTT)])
    page.wait_for_function("document.querySelector('#toast').innerText.includes('já estava')")
    assert page.locator("#queue .item").count() == 3
    assert "Carlos" in page.input_value("#vTitle")  # abre o que já existia


def test_detalhe(page):
    row(page, "Carlos").click()
    page.wait_for_selector("#transcript .seg")
    assert "orçamento" in page.inner_text("#transcript").lower()
    assert "Transcrito" in page.inner_text("#vMeta")
    assert page.is_enabled("#vAi")


def test_player(page):
    page.click("#pPlay")
    page.wait_for_timeout(1200)
    assert page.evaluate("audio.currentTime") > 0.3
    page.click("#pPlay")
    page.click("#pSpeed")
    assert page.inner_text("#pSpeed") == "1,25×" and page.evaluate("audio.playbackRate") == 1.25
    page.check("#timestamps", force=True)
    page.evaluate("audio.currentTime = 4")
    page.locator("#transcript .seg .ts").first.click()
    page.wait_for_timeout(300)
    assert page.evaluate("audio.currentTime") < 2 and page.evaluate("!audio.paused")  # clicar no tempo pula
    page.evaluate("audio.pause()")
    page.uncheck("#timestamps", force=True)
    assert page.evaluate("peaksCache.get(state.selectedId) != null")  # forma de onda


def test_titulo_e_notas(page, api):
    page.fill("#vTitle", "Orçamento cozinha")
    page.keyboard.press("Enter")
    page.fill("#vNotes", "Ligar na segunda")
    page.wait_for_timeout(1200)
    item = by_name(api, PTT.name)
    assert item["title"] == "Orçamento cozinha" and item["notes"] == "Ligar na segunda"
    assert "Orçamento cozinha" in page.inner_text("#queue")


def test_criar_cliente_pelo_menu(page, api):
    page.click("#vClient")
    page.fill(".menu-search", "Carlos Reforma")
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    assert by_name(api, PTT.name)["client_id"] is not None
    assert "Carlos Reforma" in page.inner_text("#vClient")
    assert "Carlos Reforma" in page.inner_text("#clientFilter")


def test_edicao_do_texto_salva_mesmo_trocando_de_audio(page, api):
    page.locator("#transcript .seg-text").first.click()
    page.keyboard.press("End")
    page.keyboard.type(" EDITADO")
    row(page, "Bom dia").click()  # antes do salvamento automático
    page.wait_for_timeout(1000)
    assert "EDITADO" in api.get(f"/api/transcriptions/{by_name(api, PTT.name)['id']}").json()["text"]
    row(page, "Orçamento").click()
    page.wait_for_selector("#transcript .seg")
    assert "EDITADO" in page.inner_text("#transcript")


def test_correcao_sugere_vocabulario(page, api):
    # A palavra nova da edição anterior virou sugestão para o vocabulário do cliente
    page.locator("#transcript .seg-text").first.click()
    page.keyboard.press("End")
    page.keyboard.type(" Portobello")
    page.locator("#vNotes").click()
    page.wait_for_selector("#toast.show .toast-action")
    assert "“Portobello”" in page.inner_text("#toast") and "Carlos Reforma" in page.inner_text("#toast")
    page.click("#toast .toast-action")
    page.wait_for_timeout(500)
    carlos = next(c for c in api.get("/api/clients").json() if c["name"] == "Carlos Reforma")
    assert "Portobello" in carlos["vocabulary"]
    # Palavra que já está no vocabulário não é sugerida de novo
    page.locator("#transcript .seg-text").first.click()
    page.keyboard.press("End")
    page.keyboard.type(" Portobello")
    page.locator("#vNotes").click()
    page.wait_for_timeout(400)
    assert page.locator("#toast.show .toast-action").count() == 0


def test_edicao_salva_ao_fechar_a_aba(page, api, server):
    page.locator("#transcript .seg-text").first.click()
    page.keyboard.press("End")
    page.keyboard.type(" FECHOU")
    page.goto("about:blank")  # sai da página antes do salvamento automático
    page.wait_for_timeout(1000)
    assert "FECHOU" in api.get(f"/api/transcriptions/{by_name(api, PTT.name)['id']}").json()["text"]
    page.goto(server["url"])
    page.wait_for_selector("#queue .item")
    row(page, "Orçamento").click()
    page.wait_for_selector("#transcript .seg")


def test_enviar_para_ia(page):
    page.click("#vAi")
    page.wait_for_timeout(500)
    text = clipboard(page)
    assert "Carlos Reforma" in text and "orçamento" in text.lower() and "{" not in text
    assert page.evaluate("window.__opened")[-1].startswith("https://chatgpt.com/?q=")

    page.click("#vAiMenu")
    page.locator("#menu .menu-item", has_text="Claude").click()
    page.wait_for_timeout(400)
    page.locator("#menu .menu-item", has_text="Listar tarefas").click()
    page.wait_for_timeout(400)
    assert page.evaluate("window.__opened")[-1].startswith("https://claude.ai/new?q=")
    assert "checklist" in clipboard(page)


def test_copiar_e_exportar(page):
    page.click("#vCopy")
    page.wait_for_timeout(200)
    assert "orçamento" in clipboard(page).lower()
    for label, ext in [("Baixar texto", ".txt"), ("Baixar legenda", ".srt"), ("Baixar áudio", ".ogg")]:
        page.click("#vMore")
        with page.expect_download() as d:
            page.locator("#menu .menu-item", has_text=label).click()
        content = Path(d.value.path()).read_bytes()
        assert d.value.suggested_filename.endswith(ext) and len(content) > 10
        if ext == ".srt":
            assert b"-->" in content


def test_busca_ignora_acentos_e_destaca(page):
    page.fill("#search", "reuniao")
    page.wait_for_timeout(400)
    assert page.locator("#queue .item").count() == 1
    page.locator("#queue .item").first.click()
    page.wait_for_selector("#transcript mark")
    assert page.inner_text("#transcript mark") == "reunião"
    page.fill("#search", "")
    page.wait_for_timeout(300)


def test_resolvido_e_abas(page, api):
    row(page, "Orçamento").click()
    page.click("#vResolved")
    page.wait_for_timeout(400)
    assert by_name(api, PTT.name)["resolved"]
    counts = {}
    for tab in ("pending", "resolved", "all"):
        page.click(f"#tabs .tab[data-filter={tab}]")
        counts[tab] = page.locator("#queue .item").count()
    assert counts == {"pending": 2, "resolved": 1, "all": 3}


def test_filtro_de_cliente(page):
    page.select_option("#clientFilter", label="Carlos Reforma")
    assert page.locator("#queue .item").count() == 1
    page.select_option("#clientFilter", value="none")
    assert page.locator("#queue .item").count() == 2
    page.select_option("#clientFilter", value="")


def test_atalhos(page):
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "search"
    page.keyboard.press("Escape")
    order = page.evaluate("visibleItems().map(i => i.id)")
    page.evaluate(f"select({order[0]})")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("state.selectedId") == order[1]
    page.keyboard.press("ArrowUp")
    assert page.evaluate("state.selectedId") == order[0]


def test_acoes_em_lote(page, api):
    page.click("#checkAll")
    assert page.is_visible("#bulkbar") and "3 selecionados" in page.inner_text("#bulkCount")
    page.click("#bulkAi")
    page.wait_for_timeout(400)
    text = clipboard(page)
    # Em ordem cronológica: o mais antigo (Carlos, dia 20) primeiro
    assert "[Áudio 1" in text and "[Áudio 3" in text and text.index("orçamento") < text.index("[Áudio 2")
    page.click("#bulkCopy")
    page.wait_for_timeout(200)
    assert "---" in clipboard(page)
    page.click("#bulkResolve")
    page.wait_for_timeout(400)
    assert all(i["resolved"] for i in api.get("/api/transcriptions").json())
    page.click("#bulkResolve")
    page.wait_for_timeout(400)
    assert not any(i["resolved"] for i in api.get("/api/transcriptions").json())
    page.click("#bulkClient")
    page.locator("#menu .menu-item", has_text="Carlos Reforma").click()
    page.wait_for_timeout(500)
    assert all(i["client_id"] for i in api.get("/api/transcriptions").json())
    page.keyboard.press("Escape")
    assert page.is_hidden("#bulkbar")


def test_ajustes_gerais(page, api):
    page.click("#openSettings")
    page.locator("#aiPicker button[data-ai=copy]").click()
    page.fill("#vocabulary", "Thaynara, Portobello")
    page.wait_for_timeout(1000)
    settings = api.get("/api/settings").json()
    assert settings["ai"] == "copy" and settings["vocabulary"] == "Thaynara, Portobello"


def test_ajustes_importacao_automatica(page, api, tmp_path):
    assert page.is_hidden(".watch-folder")
    page.locator("label.switch", has=page.locator("#watchEnabled")).click()
    page.wait_for_selector(".watch-folder:not([hidden])")
    assert "Downloads" in page.get_attribute("#watchFolder", "placeholder")
    page.fill("#watchFolder", str(tmp_path))
    page.keyboard.press("Tab")
    page.wait_for_timeout(400)
    s = api.get("/api/settings").json()
    assert s["watch_enabled"] and s["watch_folder"] == str(tmp_path) and s["watch_since"] > 0
    page.locator("label.switch", has=page.locator("#watchEnabled")).click()
    page.wait_for_timeout(400)
    assert not api.get("/api/settings").json()["watch_enabled"] and page.is_hidden(".watch-folder")


def test_ajustes_backup(page):
    with page.expect_download() as d:
        page.click("#backup")
    assert d.value.suggested_filename.startswith("zapscribe-backup-") and d.value.suggested_filename.endswith(".zip")
    import zipfile

    with zipfile.ZipFile(d.value.path()) as z:
        assert "zapscribe.db" in z.namelist() and sum(n.startswith("audios/") for n in z.namelist()) == 3


def test_ajustes_prompts(page, api):
    page.click("#settingsTabs .tab[data-pane=templates]")
    page.click("#newTemplate")
    page.fill("#templateForm [name=name]", "Meu prompt")
    page.fill("#templateForm [name=body]", "Cliente: {cliente}\n{texto}")
    page.locator("#templateForm button[type=submit]").click()
    page.wait_for_timeout(500)
    tid = next(t["id"] for t in api.get("/api/templates").json() if t["name"] == "Meu prompt")
    page.click("#settingsTabs .tab[data-pane=general]")
    page.select_option("#defaultTemplate", label="Meu prompt")
    page.wait_for_timeout(400)
    assert api.get("/api/settings").json()["template_id"] == tid
    page.click("#settingsTabs .tab[data-pane=templates]")
    page.locator("#templateList li", has_text="Meu prompt").click()
    page.click("#deleteTemplate")
    page.click("#confirmOk")
    page.wait_for_timeout(500)
    assert not any(t["name"] == "Meu prompt" for t in api.get("/api/templates").json())
    assert api.get("/api/settings").json()["template_id"] is None


def test_ajustes_clientes(page, api):
    page.click("#settingsTabs .tab[data-pane=clients]")
    page.click("#newClient")
    page.fill("#clientForm [name=name]", "Maria")
    page.fill("#clientForm [name=vocabulary]", "Kauê, drywall")
    page.locator("#clientForm button[type=submit]").click()
    page.wait_for_timeout(500)
    maria = next(c for c in api.get("/api/clients").json() if c["name"] == "Maria")
    assert maria["vocabulary"] == "Kauê, drywall"
    page.locator("#clientList li", has_text="Carlos Reforma").click()
    assert "3 áudios vinculados" in page.inner_text("#clientUsage")
    page.click("#deleteClient")
    page.click("#confirmOk")
    page.wait_for_timeout(500)
    assert not any(c["name"] == "Carlos Reforma" for c in api.get("/api/clients").json())
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert not page.evaluate("settingsModal.open")
    assert "Carlos Reforma" not in page.inner_text("#queue")


def test_mudanca_em_outra_aba_aparece(page, api):
    api.patch(f"/api/transcriptions/{by_name(api, VOCAB.name)['id']}", json={"title": "Mudou em outra aba"})
    page.wait_for_function("document.querySelector('#queue').innerText.includes('Mudou em outra aba')", timeout=5000)


def test_refazer_transcricao(page):
    row(page, "Orçamento").click()
    page.click("#vMore")
    page.locator("#menu .menu-item", has_text="Equilibrado").click()
    page.wait_for_timeout(300)
    assert any(s in page.inner_text("#vMeta") for s in ("Na fila", "Transcrevendo"))
    page.wait_for_function("document.querySelector('#vMeta').innerText.includes('Transcrito')", timeout=180000)
    assert page.locator("#transcript .seg").count() > 0


def test_aviso_quando_termina_em_segundo_plano(page, api):
    page.evaluate("""() => {
        window.__notes = [];
        window.Notification = class {
            static permission = 'granted';
            static requestPermission() { return Promise.resolve('granted'); }
            constructor(title, opts) { window.__notes.push([title, opts.body]); }
            close() {}
        };
        Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    }""")
    id_ = by_name(api, AUDIO.name)["id"]
    api.post(f"/api/transcriptions/{id_}/retranscribe", json={"model": "small"})
    page.wait_for_function("window.__notes.length > 0", timeout=120000)
    title, body = page.evaluate("window.__notes[0]")
    assert title == "Transcrição pronta" and body
    page.evaluate("delete document.hidden")  # volta ao normal (propriedade do protótipo)


def test_excluir(page):
    page.click("#vMore")
    page.locator("#menu .menu-item", has_text="Excluir").click()
    page.click("#confirmOk")
    page.wait_for_timeout(600)
    assert page.locator("#queue .item").count() == 2
    assert page.evaluate("state.selectedId") is not None  # seleciona o próximo
    page.click("#checkAll")
    page.click("#bulkDelete")
    page.click("#confirmOk")
    page.wait_for_timeout(600)
    assert page.locator("#queue .item").count() == 0 and page.is_visible("#emptyState")


def test_arrastar_e_soltar(page):
    dt = page.evaluate_handle(
        """(b64) => {
            const bin = atob(b64); const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            const dt = new DataTransfer(); dt.items.add(new File([arr], 'solto.ogg', {type: 'audio/ogg'})); return dt;
        }""",
        base64.b64encode(AUDIO.read_bytes()).decode(),
    )
    page.dispatch_event("body", "dragenter", {"dataTransfer": dt})
    assert page.is_visible("#dropOverlay.show")
    page.dispatch_event("body", "drop", {"dataTransfer": dt})
    page.wait_for_function("document.querySelectorAll('#queue .item').length === 1", timeout=20000)


def test_recusa_arquivo_que_nao_e_audio(page):
    page.set_input_files("#fileInput", {"name": "nota.txt", "mimeType": "text/plain", "buffer": b"oi"})
    page.wait_for_timeout(400)
    assert "Nenhum arquivo de áudio" in page.inner_text("#toast")


def test_sem_erros_no_console(page):
    assert page.errors == []
