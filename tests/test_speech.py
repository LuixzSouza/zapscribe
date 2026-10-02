"""Texto para áudio. O servidor roda com as vozes online desligadas (ZAPSCRIBE_TTS_ONLINE=0), então
os testes da API e da interface usam a voz offline e não dependem da internet. A voz online é
testada à parte e é pulada quando não há internet."""

import asyncio
import io
import json

import av
import pytest

import speech

needs_offline = pytest.mark.skipif(not speech.offline_installed(),
                                   reason="Voz offline não baixada (Configurações → Voz offline)")


@pytest.fixture(scope="module")
def server_env():
    return {"ZAPSCRIBE_TTS_ONLINE": "0"}


def duration(ogg: bytes) -> float:
    with av.open(io.BytesIO(ogg)) as f:
        stream = f.streams.audio[0]
        assert stream.codec_context.name == "opus"
        return sum(frame.samples for frame in f.decode(stream)) / stream.rate


def generate(api, **body) -> tuple[list[dict], bytes | None]:
    """Pede o áudio e devolve os eventos de progresso e o .ogg final."""
    with api.stream("POST", "/api/speech", json=body) as r:
        assert r.status_code == 200, r.read()
        events = [json.loads(line) for line in r.iter_lines() if line]
    final = events[-1]
    return events, api.get(final["url"]).content if "url" in final else None


def run(gen) -> list[dict]:
    async def collect():
        return [event async for event in gen]
    return asyncio.run(collect())


# ------------------------------------------------------------ texto

@pytest.mark.parametrize("text, expected", [
    ("Custa R$ 1.250,00.", "Custa 1250 reais."),
    ("R$ 416,67 ou R$0,50 ou R$ 1,00", "416 reais e 67 centavos ou 50 centavos ou 1 real"),
    ("Dia 03/10 às 14h30, ou 1/1/2027 às 9h", "Dia 3 de outubro às 14 horas e 30, ou 1 de janeiro de 2027 às 9 horas"),
    ("Dia 15/10/26 às 14:30", "Dia 15 de outubro de 2026 às 14 horas e 30"),
    ("Das 8:00h às 9:05", "Das 8 horas às 9 horas e 5"),
    ("Placar 2:1", "Placar 2:1"),
    ("Em 3x sem juros", "Em 3 vezes sem juros"),
    ("Liga no (11) 98765-4321", "Liga no (11) 9 87 65, 43 21"),
    ("Nada a mudar: 32/13, 25h, 2024", "Nada a mudar: 32/13, 25h, 2024"),
    ("  vários\n\nespaços  ", "vários espaços"),
])
def test_normaliza_o_que_a_voz_le_errado(text, expected):
    assert speech.normalize(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Hello, how are you?", "en"),
    ("Good morning", "en"),
    ("Thank you!", "en"),
    ("Please send me the invoice by Friday. It costs $1,250.50.", "en"),
    ("Bom dia, tudo certo.", "pt"),
    ("Oi, Carlos! O orçamento sai amanhã às 10h.", "pt"),
    ("Vou mandar o report do meeting para o time hoje", "pt"),  # palavras em inglês no meio
    ("ok", None),
    ("2024", None),
])
def test_reconhece_o_idioma(text, expected):
    assert speech.detect_language(text) == expected


def test_texto_em_ingles_troca_para_a_voz_em_ingles():
    english = "The meeting is at 10/03 and it costs R$ 5,00."
    assert speech.prepare(english, "pt-BR-AntonioNeural") == (english, "en-US-AndrewNeural", "en")  # sem "3 de outubro"
    assert speech.prepare(english, "pf_dora")[1] == "af_heart"
    assert speech.prepare(english, "pt-BR-ThalitaMultilingualNeural")[1:] == ("pt-BR-ThalitaMultilingualNeural", "en")
    assert speech.prepare("Bom dia, custa R$ 5,00", "am_michael") == ("Bom dia, custa 5 reais", "pm_alex", "pt")
    assert speech.prepare("ok", "en-US-AvaNeural")[1:] == ("en-US-AvaNeural", "en")  # sem saber: fica a escolhida


def test_divide_no_fim_das_frases():
    text = " ".join(f"Esta é a frase número {i}, que fala de um assunto qualquer." for i in range(300))
    chunks = speech.split_text(text)
    assert len(chunks) > 5 and all(len(c) <= speech.CHUNK_CHARS for c in chunks)
    assert all(c.endswith(".") for c in chunks) and " ".join(chunks) == text


def test_divide_frase_longa_sem_ponto():
    text = ", ".join(f"item {i}" for i in range(1000))  # uma frase só, sem ponto
    chunks = speech.split_text(text, 200)
    assert all(len(c) <= 200 for c in chunks) and " ".join(chunks) == text
    words = "palavra " * 500  # sem pontuação nenhuma
    assert all(len(c) <= 200 for c in speech.split_text(words.strip(), 200))


# ------------------------------------------------------------ API

def test_lista_vozes(api):
    s = api.get("/api/speech").json()
    assert s["default"] == "pt-BR-AntonioNeural" and s["online"] is False and s["max_chars"] == 100_000
    voices = {v["id"]: v for v in s["voices"]}
    assert voices["pt-BR-AntonioNeural"]["engine"] == "online" and not voices["pt-BR-AntonioNeural"]["available"]
    assert voices["pf_dora"]["available"] == s["offline_installed"]
    assert api.get("/api/settings").json()["tts_voice"] == "pt-BR-AntonioNeural"


def test_texto_invalido(api):
    assert api.post("/api/speech", json={"text": "   "}).status_code == 400
    assert api.post("/api/speech", json={"text": ""}).status_code == 422
    assert api.post("/api/speech", json={"text": "oi", "voice": "nao-existe"}).status_code == 400
    assert api.post("/api/speech", json={"text": "a" * (speech.MAX_CHARS + 1)}).status_code == 400
    assert api.post("/api/speech", json={"text": "oi", "speed": 5}).status_code == 422


def test_arquivo_inexistente(api):
    assert api.get("/api/speech/files/" + "0" * 32 + ".ogg").status_code == 404
    assert api.get("/api/speech/files/..%2Fzapscribe.db").status_code == 404


@needs_offline
def test_gera_ogg_com_a_voz_offline(api):
    events, ogg = generate(api, text="Oi, tudo bem? O orçamento fica em R$ 350,00.", voice="pf_dora")
    assert events[0] == {"done": 0, "total": 1} and events[-1]["voice"] == "pf_dora"
    assert ogg[:4] == b"OggS" and 2 < duration(ogg) < 10
    assert abs(events[-1]["duration"] - duration(ogg)) < 0.1


@needs_offline
def test_sem_voz_online_usa_a_offline_do_mesmo_genero(api):
    events, _ = generate(api, text="Bom dia, tudo certo.", voice="pt-BR-AntonioNeural")
    assert events[-1]["voice"] == "pm_alex"
    events, _ = generate(api, text="Bom dia, tudo certo.", voice="pt-BR-FranciscaNeural")
    assert events[-1]["voice"] == "pf_dora"


@needs_offline
def test_texto_em_ingles_sai_em_ingles(api):
    text = "Hello! This is a test of the English voice, and it should sound natural."
    events, ogg = generate(api, text=text, voice="pt-BR-AntonioNeural")  # online desligada + inglês
    assert events[-1]["voice"] == "am_michael" and 2 < duration(ogg) < 12
    events, _ = generate(api, text=text, voice="pf_dora")
    assert events[-1]["voice"] == "af_heart"


@needs_offline
def test_velocidade_e_reaproveitamento(api, server):
    text = "Esta frase serve para medir a velocidade da fala."
    _, normal = generate(api, text=text, voice="pm_alex")
    _, fast = generate(api, text=text, voice="pm_alex", speed=1.25)
    assert duration(fast) < duration(normal) * 0.9
    files = sorted((server["data"] / "speech").glob("*.ogg"))
    events, again = generate(api, text=text, voice="pm_alex")
    assert again == normal and len(events) == 1  # já pronto: nem passa pelo progresso
    assert sorted((server["data"] / "speech").glob("*.ogg")) == files


@needs_offline
def test_texto_longo_em_partes(tmp_path, monkeypatch):
    """Partes pequenas para o teste ser rápido: o progresso vem a cada parte e o áudio sai inteiro."""
    monkeypatch.setattr(speech, "SPEECH_DIR", tmp_path)
    monkeypatch.setattr(speech, "PARTS_DIR", tmp_path / "parts")
    monkeypatch.setattr(speech, "CHUNK_CHARS", 40)
    text = "Primeiro assunto do dia. Segundo assunto, bem rápido. Terceiro e último assunto da conversa."
    events = run(speech.synthesize(text, "pf_dora"))
    progress = [e for e in events if "done" in e]
    assert progress[0] == {"done": 0, "total": 3} and progress[-1] == {"done": 3, "total": 3}
    assert {"joining": True} in events
    ogg = (tmp_path / events[-1]["file"]).read_bytes()
    assert duration(ogg) > 4 and not list((tmp_path / "parts").iterdir())  # partes apagadas ao juntar


@needs_offline
def test_partes_prontas_sao_reaproveitadas(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "SPEECH_DIR", tmp_path)
    monkeypatch.setattr(speech, "PARTS_DIR", tmp_path / "parts")
    monkeypatch.setattr(speech, "CHUNK_CHARS", 40)
    text = "Uma frase curta aqui. Outra frase curta ali. E mais uma no fim."
    calls = []
    real = speech._offline

    async def failing(chunk, voice, speed):
        calls.append(chunk)
        if len(calls) == 3:
            raise RuntimeError("caiu no meio")
        return await real(chunk, voice, speed)

    monkeypatch.setattr(speech, "_offline", failing)
    with pytest.raises(RuntimeError):
        run(speech.synthesize(text, "pf_dora"))
    assert len(list((tmp_path / "parts").iterdir())) == 2  # as duas primeiras ficaram guardadas
    calls.clear()
    events = run(speech.synthesize(text, "pf_dora"))
    assert len(calls) == 1 and "file" in events[-1]  # só gerou a que faltava


# ------------------------------------------------------------ voz online (precisa de internet)

def test_voz_online_texto_longo(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "SPEECH_DIR", tmp_path)
    monkeypatch.setattr(speech, "PARTS_DIR", tmp_path / "parts")
    monkeypatch.setattr(speech, "ONLINE", True)
    monkeypatch.setattr(speech, "offline_installed", lambda: False)  # sem reserva: um erro aparece
    text = " ".join(f"Este é o parágrafo {i} do texto longo, lido pela voz online." for i in range(60))
    try:
        events = run(speech.synthesize(text, "pt-BR-AntonioNeural"))
    except speech.SpeechError as e:
        pytest.skip(f"Sem acesso à voz online: {e}")
    assert events[0]["total"] == len(speech.split_text(text)) > 1
    assert events[-1]["voice"] == "pt-BR-AntonioNeural"
    assert duration((tmp_path / events[-1]["file"]).read_bytes()) > 60


# ------------------------------------------------------------ interface

@needs_offline
def test_interface(page):
    page.click("#openSpeech")
    page.wait_for_selector("#speechModal[open]")
    assert page.is_disabled("#speechGo")
    assert page.locator("#speechVoice option[value='pt-BR-AntonioNeural']").is_disabled()  # online desligada
    assert page.input_value("#speechVoice") in ("pf_dora", "pm_alex")

    page.fill("#speechText", "Oi, Carlos! O orçamento sai amanhã às 10h.")
    assert page.inner_text("#speechCount").startswith("42")
    page.locator("#speechSpeed button", has_text="1,1×").click()
    page.wait_for_function("document.querySelector('#speechSpeed [aria-checked=true]').textContent === '1,1×'")
    page.press("#speechText", "Control+Enter")
    page.wait_for_selector("#speechAudio:not([hidden])", timeout=60000)
    page.wait_for_function("document.querySelector('#speechAudio').duration > 1")
    assert "Pronto" in page.inner_text("#speechState") and page.is_disabled("#speechGo")

    with page.expect_download() as d:
        page.click("#speechDownload")
    assert d.value.suggested_filename == "Áudio - Oi, Carlos! O orçamento sai amanhã.ogg"

    page.fill("#speechText", "Outro texto")  # mudou o texto: o áudio antigo some
    assert page.is_hidden("#speechAudio") and page.is_hidden("#speechDownload") and page.is_enabled("#speechGo")
    assert "mudaram" in page.inner_text("#speechState")
    page.fill("#speechText", "Oi, Carlos! O orçamento sai amanhã às 10h.")  # voltou: o áudio volta
    assert page.is_visible("#speechAudio")
    page.click("#speechModal [data-close]")
    assert page.evaluate("fetch('/api/settings').then(r => r.json()).then(s => s.tts_speed)") == 1.1


@needs_offline
def test_texto_e_audio_continuam_depois_de_recarregar(page, server):
    page.goto(server["url"])
    page.click("#openSpeech")
    page.wait_for_selector("#speechModal[open]")
    assert page.input_value("#speechText") == "Oi, Carlos! O orçamento sai amanhã às 10h."
    page.wait_for_selector("#speechAudio:not([hidden])")


def test_desfazer_ao_apagar_sem_querer(page):
    text = "Um texto comprido que eu escrevi com calma e não quero perder de jeito nenhum, " * 3
    page.fill("#speechText", text)
    page.fill("#speechText", "")  # selecionou tudo e apagou
    assert page.is_visible("#speechUndo") and "caracteres apagados" in page.inner_text("#speechUndo")
    page.click("#speechUndo button")
    assert page.input_value("#speechText") == text and page.is_hidden("#speechUndo")
    page.fill("#speechText", text + "!")  # digitar normalmente não oferece desfazer
    assert page.is_hidden("#speechUndo")


def test_selecionar_e_soltar_fora_nao_fecha(page):
    box = page.locator("#speechText").bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 10)
    page.mouse.down()
    page.mouse.move(5, 5)  # arrastou a seleção para fora da janela
    page.mouse.up()
    assert page.locator("#speechModal").get_attribute("open") is not None
    page.mouse.click(5, 5)  # um clique fora de verdade fecha
    assert page.locator("#speechModal").get_attribute("open") is None


@needs_offline
def test_fechar_a_janela_nao_cancela(page):
    page.click("#openSpeech")
    page.fill("#speechText", "Uma frase nova para gerar com a janela fechada.")
    page.click("#speechGo")
    page.wait_for_selector("#openSpeech.busy")
    page.keyboard.press("Escape")
    page.wait_for_function("document.querySelector('#toast').innerText.includes('Áudio pronto')", timeout=60000)
    assert page.locator("#openSpeech.busy").count() == 0
    page.click("#toast .toast-action")  # "Abrir"
    page.wait_for_selector("#speechModal[open] #speechAudio:not([hidden])")
    page.click("#speechModal [data-close]")


def test_ouvir_substitui_o_texto_com_desfazer(page):
    page.evaluate("openSpeech('Resposta da IA para ouvir')")
    page.wait_for_selector("#speechModal[open]")
    assert page.input_value("#speechText") == "Resposta da IA para ouvir"
    assert "substituído" in page.inner_text("#speechUndo")
    page.click("#speechUndo button")
    assert page.input_value("#speechText") == "Uma frase nova para gerar com a janela fechada."
    page.click("#speechModal [data-close]")


@needs_offline
def test_interface_texto_em_ingles(page):
    assert page.locator("#speechVoice option[value='am_michael']").inner_text() == "Michael · inglês"
    page.evaluate("openSpeech('Hello! How are you doing today?', { auto: true })")
    page.wait_for_selector("#speechAudio:not([hidden])", timeout=60000)
    assert "Texto em inglês: lido pela voz" in page.inner_text("#speechNote")
    page.click("#speechModal [data-close]")


def test_sem_erros_no_console(page):
    assert page.errors == []
