"""Importa sozinho os áudios do WhatsApp salvos numa pasta (a Downloads, por padrão).

Só entram arquivos salvos depois que a opção foi ligada, e cada um uma vez só: a data do
último importado fica guardada nos ajustes (watch_since). Assim, excluir um áudio da lista
não faz ele voltar na próxima verificação.
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

import anyio

import db

log = logging.getLogger("zapscribe")

# "WhatsApp Ptt 2026-09-20 at 14.35.12.ogg", "WhatsApp Audio 2026-09-21 at 09.05.33 (1).opus"…
WHATSAPP_FILE = re.compile(r"^WhatsApp (Ptt|Audio|Áudio)\b.*\.(ogg|opus|m4a|mp3|aac|amr|wav|mp4)$", re.IGNORECASE)
INTERVAL = float(os.environ.get("ZAPSCRIBE_WATCH_INTERVAL", "3"))
# Espera o arquivo ficar este tempo sem mudar, para não pegar um download pela metade
SETTLE_SECONDS = 2


def default_folder() -> Path:
    return Path.home() / "Downloads"


def new_files(folder: Path, since: float) -> list[Path]:
    now = time.time()
    found = []
    for path in folder.iterdir():
        if not WHATSAPP_FILE.match(path.name) or not path.is_file():
            continue
        mtime = path.stat().st_mtime
        if since < mtime < now - SETTLE_SECONDS:
            found.append((mtime, path))
    return [path for _, path in sorted(found)]


async def run(import_file: Callable[[Path], dict]) -> None:
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            settings = db.get_settings()
            if not settings["watch_enabled"]:
                continue
            folder = Path(settings["watch_folder"]) if settings["watch_folder"] else default_folder()
            if not folder.is_dir():
                continue
            for path in await anyio.to_thread.run_sync(new_files, folder, settings["watch_since"] or 0):
                mtime = path.stat().st_mtime
                try:
                    await anyio.to_thread.run_sync(import_file, path)
                    log.info("Importado da pasta: %s", path.name)
                except Exception:
                    log.exception("Não foi possível importar %s", path.name)
                finally:
                    # Avança mesmo se falhar, para não tentar o mesmo arquivo para sempre
                    db.save_settings({"watch_since": mtime})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Falha ao verificar a pasta de áudios")
