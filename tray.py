"""Zapscribe sem janela de terminal: fica como um ícone ao lado do relógio do Windows.

    uv run pythonw tray.py            (o iniciar.bat faz isso)
    uv run pythonw tray.py --startup  (ao ligar o computador: não abre o navegador)
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# Sem terminal (pythonw), stdout/stderr não existem e o servidor quebraria ao escrever o log
if sys.stdout is None or sys.stderr is None:
    log_dir = Path(os.environ.get("ZAPSCRIBE_DATA_DIR") or ROOT / "data")
    log_dir.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(log_dir / "zapscribe.log", "a", encoding="utf-8", buffering=1)

import threading  # noqa: E402
import webbrowser  # noqa: E402

import pystray  # noqa: E402
import uvicorn  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import main  # noqa: E402

PORT = int(os.environ.get("ZAPSCRIBE_PORT", "8000"))
URL = f"http://127.0.0.1:{PORT}"
STARTUP_DIR = Path(
    os.environ.get("ZAPSCRIBE_STARTUP_DIR")
    or Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
)
STARTUP_FILE = STARTUP_DIR / "Zapscribe.vbs"


def startup_enabled() -> bool:
    return STARTUP_FILE.exists()


def set_startup(enabled: bool) -> None:
    """Liga ou desliga o Zapscribe ao iniciar o Windows (um atalho na pasta Inicializar)."""
    if not enabled:
        STARTUP_FILE.unlink(missing_ok=True)
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
    # .vbs roda o comando sem abrir janela; o 0 no fim esconde qualquer terminal
    command = f'"{pythonw}" "{ROOT / "tray.py"}" --startup'.replace('"', '""')
    STARTUP_FILE.write_text(
        f'CreateObject("WScript.Shell").Run "{command}", 0, False\r\n', encoding="utf-16"
    )


def icon_image(size: int = 64) -> Image.Image:
    """O mesmo desenho do logo: barras de áudio brancas num quadrado verde."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 4, fill="#0f766e")
    bars = [0.25, 0.55, 0.85, 0.45, 0.7, 0.25]
    width = size / 16
    gap = (size - 2 * size * 0.2 - width * len(bars)) / (len(bars) - 1)
    x = size * 0.2
    for h in bars:
        half = h * size * 0.32
        draw.rounded_rectangle((x, size / 2 - half, x + width, size / 2 + half), radius=width / 2, fill="white")
        x += width + gap
    return img


def run() -> None:
    open_browser = "--startup" not in sys.argv
    if main.port_in_use(PORT):
        # Já está rodando: só abre a página
        if open_browser:
            webbrowser.open(URL)
        return

    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    if open_browser:
        main.open_browser_when_ready(URL, PORT)

    def quit_app(_icon, _item):
        server.should_exit = True  # o ícone some quando o servidor terminar (watch_server)

    def toggle_startup(_icon, _item):
        set_startup(not startup_enabled())

    icon = pystray.Icon(
        "zapscribe",
        icon_image(),
        f"Zapscribe · {URL}",
        menu=pystray.Menu(
            pystray.MenuItem("Abrir Zapscribe", lambda: webbrowser.open(URL), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Iniciar com o Windows", toggle_startup, checked=lambda _item: startup_enabled()),
            pystray.MenuItem("Sair", quit_app),
        ),
    )

    def watch_server(icon):
        icon.visible = True
        thread.join()  # se o servidor parar sozinho (ex.: erro ao iniciar), o ícone sai junto
        icon.stop()

    icon.run(setup=watch_server)


if __name__ == "__main__":
    run()
