# Zapscribe 🎙️

Transcreve áudios do WhatsApp (e qualquer outro áudio ou vídeo) e manda o texto direto para a IA.
Roda 100% no seu computador com [faster-whisper](https://github.com/SYSTRAN/faster-whisper): nada é enviado para a internet.

## Como usar

Dê dois cliques em `iniciar.bat` (ou rode `uv run main.py`) e acesse http://127.0.0.1:8000.

1. Arraste os áudios para a janela (ou use Ctrl+V). Até 3 são transcritos ao mesmo tempo.
2. Clique em **Enviar para IA**: o prompt é montado com a transcrição, copiado e aberto no ChatGPT ou Claude.
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

## Estrutura

| Arquivo          | O que faz                                                    |
|------------------|--------------------------------------------------------------|
| `main.py`        | API (FastAPI): transcrições, clientes, prompts e ajustes      |
| `db.py`          | Banco SQLite                                                 |
| `transcriber.py` | Fila de transcrição em segundo plano e eventos em tempo real |
| `static/`        | Interface (HTML, CSS e JS, sem build)                        |

Ícones: [Lucide](https://lucide.dev) (ISC). Fonte: [Geist](https://vercel.com/font) (OFL).
