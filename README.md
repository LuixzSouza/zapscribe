# Zapscribe 🎙️

Converte áudios (WhatsApp `.ogg/.opus`, `.mp3`, `.m4a`, `.wav`, vídeos...) em texto, 100% local, usando [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

## Como usar

Dê dois cliques em `iniciar.bat` (ou rode `uv run main.py`) e acesse http://127.0.0.1:8000.

Arraste (ou cole com Ctrl+V) quantos áudios quiser. Até 3 são transcritos ao mesmo tempo e o resto espera na fila.
O texto aparece enquanto é transcrito e pode ser editado, copiado ou baixado como `.txt`, um por um ou todos de uma vez.

## Modelos

Medido em um Ryzen 5 5600 com um áudio de 69 s:

| Opção        | Modelo           | Tempo  | Observação                          |
|--------------|------------------|--------|-------------------------------------|
| Rápido       | `small`          | 6 s    | Padrão, pode errar algumas palavras |
| Equilibrado  | `medium`         | 18 s   |                                     |
| Mais preciso | `large-v3-turbo` | 23 s   | Use o botão "Refazer com precisão"  |

Na primeira vez que um modelo é usado, ele é baixado para a pasta `models/`.
O número de áudios em paralelo fica em `PARALLEL`, no arquivo `main.py`.
