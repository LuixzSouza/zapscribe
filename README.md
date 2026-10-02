# Zapscribe 🎙️

Transcreve áudios do WhatsApp (e qualquer outro áudio ou vídeo) e manda o texto direto para a IA.
Roda 100% no seu computador com [faster-whisper](https://github.com/SYSTRAN/faster-whisper): nada é enviado para a internet.
Também faz o caminho inverso: transforma texto em áudio (veja **Texto para áudio** abaixo).

## Como usar

Dê dois cliques em `iniciar.bat`. O Zapscribe abre sem janela de terminal, como um ícone ao lado do relógio
do Windows, e o navegador abre sozinho em http://127.0.0.1:8000 quando estiver pronto.
Clique com o botão direito no ícone para **Abrir**, ligar **Iniciar com o Windows** ou **Sair**.

Para ver os registros no terminal, rode `uv run main.py` (sem o ícone). Sem terminal, eles ficam em `data/zapscribe.log`.

1. Arraste os áudios para a janela (ou use Ctrl+V). Até 3 são transcritos ao mesmo tempo.
2. Clique em **Enviar para IA**: o prompt é montado com a transcrição, copiado e aberto no ChatGPT ou Claude,
   ou respondido ali mesmo pela **IA local**.
3. Marque como **Resolvido** para tirar da aba “Pendentes”.

## Funcionalidades

- **Histórico salvo** (SQLite em `data/`): áudios e transcrições ficam guardados, e a fila continua depois de reiniciar.
- **Clientes**: vincule cada áudio a um cliente, filtre por cliente e cadastre telefone e observações.
- **Prompts personalizáveis** com as variáveis `{texto}`, `{cliente}`, `{data}`, `{titulo}` e `{notas}`.
- **Vários áudios de uma vez**: selecione e envie todos juntos para a IA, em ordem cronológica.
- **Edição**: título (gerado automaticamente a partir da fala), texto da transcrição e notas, com salvamento automático.
- **Busca** em títulos, textos, notas e nomes de clientes.
- **Player** com forma de onda, velocidade de 1× a 2× e clique no trecho para ouvir.
- **Exportação**: `.txt`, legenda `.srt`, texto com tempos ou o áudio original.
- **Refazer** a transcrição com um modelo mais preciso.
- **Vocabulário**: cadastre nomes e termos (em Ajustes e em cada cliente) para a transcrição escrevê-los certo.
  Ex.: sem vocabulário saiu “Tainara… porcelanato portubelo”; com ele, “Thaynara… porcelanato Portobello”.
  Ao corrigir uma palavra no texto, o app oferece adicioná-la ao vocabulário.
- **Importação automática** (em Ajustes): áudios do WhatsApp salvos na pasta Downloads entram na fila sozinhos.
- **Aviso do Windows** quando uma transcrição termina com a aba em segundo plano.
- **Texto para áudio** (botão no topo, ou **Ouvir** na resposta da IA local): escreva ou cole um texto e
  gere um áudio `.ogg` (o formato dos áudios do WhatsApp) para ouvir ou baixar.
  - Vozes **online** (Antonio, Francisca e Thalita, da Microsoft): as mais naturais. Precisam de internet e
    **o texto é enviado para a Microsoft**.
  - Voz **offline** (Dora e Alex, com o [Kokoro](https://github.com/thewh1teagle/kokoro-onnx)): roda no
    computador, sem internet. Baixe em Ajustes (350 MB). Quando a voz online não responde, ela é usada sozinha.
  - **Textos em inglês**: o idioma é reconhecido sozinho e o texto é lido por uma voz em inglês do mesmo tipo
    (Andrew e Ava online; Michael e Heart offline), sem sotaque. Também dá para escolher essas vozes na lista;
    a Thalita é multilíngue e lê os dois idiomas.
  - **Textos longos** (até 100.000 caracteres, ~2 horas de fala): divididos em partes no fim das frases,
    geradas em paralelo e juntadas num áudio só, com o progresso na tela. Medido: 11.600 caracteres
    (16 min de áudio) em ~16 s com a voz online; a voz offline fala ~3× mais rápido que o tempo real.
    Se cair no meio, gerar de novo reaproveita as partes já prontas.
  - **Nada se perde sem querer**: o texto fica salvo no navegador (mesmo fechando a página), apagar muito
    texto de uma vez oferece **Desfazer**, e fechar a janela não interrompe a geração (avisa quando terminar).
  - Valores, datas, horas e telefones são lidos por extenso (“R$ 1.250,00” → “1250 reais”, “14h30” → “14 horas e 30”).
  - Para nunca usar as vozes online: `ZAPSCRIBE_TTS_ONLINE=0`.
- **Backup** em Ajustes: banco e áudios num `.zip` (para restaurar, descompacte na pasta `data/`).
- **IA local** (em Ajustes → Enviar para → IA local): resume e sugere a resposta sem internet, usando o
  [Ollama](https://ollama.com). A resposta aparece enquanto é escrita e fica salva no áudio. Se não houver
  nenhum modelo, os Ajustes oferecem baixar o recomendado (`qwen3:4b`, 2,5 GB).
- **Data certa do áudio**: vem do nome do arquivo do WhatsApp (`WhatsApp Audio 2026-09-21 at 09.05.33` ou,
  do Android, `PTT-20260921-WA0012`). Quando o nome só traz o dia, o horário vem do arquivo se for do mesmo dia;
  senão o app mostra só o dia, em vez de um horário errado.
- **Sem duplicados**: mandar o mesmo áudio de novo abre o que já existe.
- **Histórico grande**: com 3.000 áudios, a página abre em menos de 1 s e a busca responde em ~0,1 s.
- **Placa de vídeo NVIDIA** é usada automaticamente quando disponível (com as bibliotecas CUDA instaladas);
  se não funcionar, a transcrição continua na CPU. Para forçar: `ZAPSCRIBE_DEVICE=cpu` ou `cuda`.
- **Protegido contra outros sites**: só a própria página, em `localhost`, consegue usar a API.

Atalhos: `/` busca · `↑` `↓` navega · `Espaço` toca/pausa · `I` envia para a IA · `Esc` limpa a seleção.

## Modelos

Medido em um Ryzen 5 5600 com um áudio de 69 s:

| Opção        | Modelo           | Tempo | Observação                          |
|--------------|------------------|-------|-------------------------------------|
| Rápido       | `small`          | 6 s   | Padrão, pode errar algumas palavras |
| Equilibrado  | `medium`         | 18 s  |                                     |
| Preciso      | `large-v3-turbo` | 23 s  | Use em “Refazer transcrição”        |

Na primeira vez que um modelo é usado, ele é baixado para a pasta `models/`.
O número de áudios em paralelo fica em `PARALLEL`, no arquivo `transcriber.py`.

## Testes

```
uv sync
uv run playwright install chromium   # só se não tiver o Edge ou o Chrome instalado
uv run pytest
```

Os testes sobem o app de verdade, com banco temporário (nada em `data/` é alterado), transcrevem os áudios
de `tests/audios/` e usam a interface num navegador sem janela. A IA local é testada com um Ollama falso
(`tests/fake_ollama.py`), sem baixar modelos. O texto para áudio usa a voz offline (os testes dela são pulados
se ela não estiver baixada) e a voz online só é testada quando há internet. Levam cerca de 3 minutos.

## Estrutura

| Arquivo          | O que faz                                                    |
|------------------|--------------------------------------------------------------|
| `main.py`        | API (FastAPI): transcrições, clientes, prompts e ajustes      |
| `db.py`          | Banco SQLite                                                 |
| `transcriber.py` | Fila de transcrição em segundo plano e eventos em tempo real |
| `watcher.py`     | Importação automática dos áudios salvos numa pasta           |
| `local_ai.py`    | IA local pelo Ollama                                         |
| `speech.py`      | Texto para áudio (vozes online e offline)                    |
| `tray.py`        | Ícone ao lado do relógio e “Iniciar com o Windows”           |
| `static/`        | Interface (HTML, CSS e JS, sem build)                        |
| `tests/`         | Testes da API, da interface e de reinício da fila            |

Ícones: [Lucide](https://lucide.dev) (ISC). Fonte: [Geist](https://vercel.com/font) (OFL).
