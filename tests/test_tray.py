"""Modo sem terminal (ícone ao lado do relógio) e a opção de iniciar com o Windows."""

import os
import subprocess
import sys
import time

import httpx
import pytest

import tray
from conftest import ROOT, free_port

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Recurso do Windows")


def test_desenho_do_icone():
    img = tray.icon_image()
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert img.getpixel((29, 32))[:3] == (255, 255, 255)  # barra branca
    assert img.getpixel((5, 32))[:3] == (15, 118, 110)    # fundo verde


def test_iniciar_com_o_windows_liga_e_desliga(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "STARTUP_DIR", tmp_path)
    monkeypatch.setattr(tray, "STARTUP_FILE", tmp_path / "Zapscribe.vbs")
    assert not tray.startup_enabled()
    tray.set_startup(True)
    assert tray.startup_enabled()
    script = (tmp_path / "Zapscribe.vbs").read_text(encoding="utf-16")
    assert "pythonw.exe" in script and "tray.py" in script and "--startup" in script and ", 0, False" in script
    tray.set_startup(False)
    assert not tray.startup_enabled()


def test_atalho_do_windows_sobe_o_servidor_sem_terminal(tmp_path, monkeypatch):
    """Executa o .vbs como o Windows faria ao ligar o computador."""
    monkeypatch.setattr(tray, "STARTUP_DIR", tmp_path / "startup")
    monkeypatch.setattr(tray, "STARTUP_FILE", tmp_path / "startup" / "Zapscribe.vbs")
    tray.set_startup(True)

    port = free_port()
    data = tmp_path / "data"
    env = {**os.environ, "ZAPSCRIBE_DATA_DIR": str(data), "ZAPSCRIBE_PORT": str(port)}
    subprocess.run(["wscript", str(tray.STARTUP_FILE)], env=env, check=True, timeout=30)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                assert httpx.get(f"{url}/api/config", timeout=1).status_code == 200
                break
            except httpx.TransportError:
                time.sleep(0.2)
        else:
            pytest.fail("O servidor não subiu pelo atalho")
        # Sem terminal, os registros vão para um arquivo
        assert (data / "zapscribe.log").exists()
        # Abrir de novo com ele rodando só termina (não cria um segundo servidor)
        again = subprocess.run([sys.executable, str(ROOT / "tray.py"), "--startup"], env=env, timeout=30)
        assert again.returncode == 0
    finally:
        pid = next(
            (line.split()[-1] for line in subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout.splitlines()
             if f"127.0.0.1:{port}" in line and "LISTENING" in line),
            None,
        )
        if pid:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
