"""Banco SQLite do Zapscribe: transcrições, clientes, modelos de prompt e ajustes."""

import json
import os
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).parent
# ZAPSCRIBE_DATA_DIR permite usar outra pasta (os testes usam uma temporária)
DATA_DIR = Path(os.environ.get("ZAPSCRIBE_DATA_DIR") or BASE_DIR / "data")
AUDIO_DIR = DATA_DIR / "audios"
DB_PATH = DATA_DIR / "zapscribe.db"

TRANSCRIPTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS transcriptions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    title_auto         INTEGER NOT NULL DEFAULT 1,
    original_name      TEXT NOT NULL,
    file_name          TEXT NOT NULL,
    size               INTEGER NOT NULL,
    duration           REAL NOT NULL DEFAULT 0,
    recorded_at        REAL,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    status             TEXT NOT NULL DEFAULT 'queued',
    progress           REAL NOT NULL DEFAULT 0,
    model              TEXT NOT NULL,
    language           TEXT NOT NULL,
    detected_language  TEXT,
    segments           TEXT NOT NULL DEFAULT '[]',
    text               TEXT NOT NULL DEFAULT '',
    elapsed            REAL NOT NULL DEFAULT 0,
    error              TEXT NOT NULL DEFAULT '',
    client_id          INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    notes              TEXT NOT NULL DEFAULT '',
    resolved           INTEGER NOT NULL DEFAULT 0,
    sha256             TEXT,
    ai_result          TEXT NOT NULL DEFAULT '',
    ai_model           TEXT NOT NULL DEFAULT ''
)"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    vocabulary  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

{TRANSCRIPTIONS_TABLE};

CREATE TABLE IF NOT EXISTS templates (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    body      TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

DEFAULT_TEMPLATES = [
    (
        "Resumir e sugerir resposta",
        "Recebi a mensagem de voz abaixo de {cliente} ({data}). "
        "Resuma em tópicos curtos o que a pessoa está pedindo e depois sugira uma resposta "
        "educada e objetiva para eu enviar pelo WhatsApp.\n\n"
        "Transcrição:\n\"\"\"\n{texto}\n\"\"\"",
    ),
    (
        "Listar tarefas e prazos",
        "Leia a transcrição da mensagem de {cliente} e liste, em formato de checklist, "
        "tudo o que eu preciso fazer, com prazos e valores quando forem citados. "
        "No final, aponte qualquer informação que ficou ambígua.\n\n"
        "Transcrição:\n\"\"\"\n{texto}\n\"\"\"",
    ),
    (
        "Responder de forma curta",
        "Escreva uma resposta curta, cordial e natural (tom de WhatsApp) para esta mensagem "
        "de {cliente}. Não invente informações que não estão no texto.\n\n"
        "Mensagem:\n\"\"\"\n{texto}\n\"\"\"",
    ),
    ("Somente a transcrição", "{texto}"),
]

DEFAULT_SETTINGS = {
    "ai": "chatgpt",           # chatgpt | claude | local | copy
    "local_model": "",         # modelo do Ollama (vazio = o menor instalado)
    "template_id": None,       # modelo usado no botão "Enviar para IA"
    "resolve_on_send": False,  # marcar como resolvido ao enviar para a IA
    "vocabulary": "",          # nomes e termos que a transcrição deve reconhecer
    "watch_enabled": False,    # importar sozinho os áudios do WhatsApp salvos numa pasta
    "watch_folder": "",        # vazio = pasta Downloads
    "watch_since": 0,          # só importa arquivos salvos depois disto (controlado pelo servidor)
}

SUMMARY_COLUMNS = (
    "id, title, title_auto, original_name, size, duration, recorded_at, created_at, updated_at, "
    "status, progress, model, language, detected_language, text, elapsed, error, client_id, notes, resolved"
)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.execute("PRAGMA journal_mode = WAL")
    _migrate_autoincrement()
    _conn.executescript(SCHEMA)
    # Colunas criadas depois da primeira versão
    _add_column("transcriptions", "sha256", "TEXT")
    _add_column("clients", "vocabulary", "TEXT NOT NULL DEFAULT ''")
    _add_column("transcriptions", "ai_result", "TEXT NOT NULL DEFAULT ''")
    _add_column("transcriptions", "ai_model", "TEXT NOT NULL DEFAULT ''")
    _conn.execute("CREATE INDEX IF NOT EXISTS transcriptions_sha256 ON transcriptions (sha256)")
    if not _conn.execute("SELECT 1 FROM templates LIMIT 1").fetchone():
        _conn.executemany(
            "INSERT INTO templates (name, body, position) VALUES (?, ?, ?)",
            [(name, body, i) for i, (name, body) in enumerate(DEFAULT_TEMPLATES)],
        )
    _conn.commit()


def _migrate_autoincrement() -> None:
    """Bancos antigos reaproveitavam o id de um áudio excluído no próximo envio, e o
    navegador e a fila podiam confundir um áudio com o outro. Recria a tabela com
    AUTOINCREMENT, mantendo os dados."""
    row = _conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transcriptions'").fetchone()
    if not row or "AUTOINCREMENT" in row["sql"]:
        return
    _conn.execute("BEGIN")
    try:
        _conn.execute("ALTER TABLE transcriptions RENAME TO transcriptions_old")
        _conn.execute(TRANSCRIPTIONS_TABLE)
        columns = ", ".join(r["name"] for r in _conn.execute("PRAGMA table_info(transcriptions_old)"))
        _conn.execute(f"INSERT INTO transcriptions ({columns}) SELECT {columns} FROM transcriptions_old")
        _conn.execute("DROP TABLE transcriptions_old")
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


def _add_column(table: str, column: str, definition: str) -> None:
    if column not in {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(row) for row in _conn.execute(sql, args).fetchall()]


def _execute(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def _update(table: str, id_: int, fields: dict) -> None:
    if not fields:
        return
    sets = ", ".join(f"{key} = ?" for key in fields)
    _execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), id_))


# ---------------------------------------------------------------- transcrições

def _transcription(row: dict) -> dict:
    row["resolved"] = bool(row["resolved"])
    row["title_auto"] = bool(row["title_auto"])
    if "segments" in row:
        row["segments"] = json.loads(row["segments"])
    return row


def list_transcriptions() -> list[dict]:
    rows = _query(
        f"SELECT {SUMMARY_COLUMNS} FROM transcriptions "
        "ORDER BY COALESCE(recorded_at, created_at) DESC, id DESC"
    )
    return [_transcription(r) for r in rows]


def get_transcription(id_: int, full: bool = True) -> dict | None:
    columns = f"{SUMMARY_COLUMNS}, segments, file_name, ai_result, ai_model" if full else SUMMARY_COLUMNS
    rows = _query(f"SELECT {columns} FROM transcriptions WHERE id = ?", (id_,))
    return _transcription(rows[0]) if rows else None


def create_transcription(**fields) -> int:
    now = time.time()
    fields.setdefault("created_at", now)
    fields.setdefault("updated_at", now)
    columns = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    return _execute(f"INSERT INTO transcriptions ({columns}) VALUES ({marks})", tuple(fields.values())).lastrowid


def update_transcription(id_: int, **fields) -> None:
    if "segments" in fields:
        fields["segments"] = json.dumps(fields["segments"], ensure_ascii=False)
    for key in ("resolved", "title_auto"):
        if key in fields:
            fields[key] = int(fields[key])
    fields["updated_at"] = time.time()
    _update("transcriptions", id_, fields)


def delete_transcription(id_: int) -> str | None:
    rows = _query("SELECT file_name FROM transcriptions WHERE id = ?", (id_,))
    _execute("DELETE FROM transcriptions WHERE id = ?", (id_,))
    return rows[0]["file_name"] if rows else None


def find_by_hash(sha256: str) -> int | None:
    rows = _query("SELECT id FROM transcriptions WHERE sha256 = ? ORDER BY id LIMIT 1", (sha256,))
    return rows[0]["id"] if rows else None


def unfinished_ids() -> list[int]:
    rows = _query("SELECT id FROM transcriptions WHERE status IN ('queued', 'running') ORDER BY id")
    return [r["id"] for r in rows]


# ---------------------------------------------------------------- clientes

def list_clients() -> list[dict]:
    return _query(
        "SELECT c.*, COUNT(t.id) AS count FROM clients c "
        "LEFT JOIN transcriptions t ON t.client_id = c.id "
        "GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
    )


def get_client(id_: int) -> dict | None:
    rows = _query(
        "SELECT c.*, (SELECT COUNT(*) FROM transcriptions WHERE client_id = c.id) AS count "
        "FROM clients c WHERE c.id = ?",
        (id_,),
    )
    return rows[0] if rows else None


def create_client(name: str, phone: str = "", notes: str = "", vocabulary: str = "") -> int:
    return _execute(
        "INSERT INTO clients (name, phone, notes, vocabulary, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, phone, notes, vocabulary, time.time()),
    ).lastrowid


def update_client(id_: int, **fields) -> None:
    _update("clients", id_, fields)


def delete_client(id_: int) -> None:
    _execute("DELETE FROM clients WHERE id = ?", (id_,))


# ---------------------------------------------------------------- modelos de prompt

def list_templates() -> list[dict]:
    return _query("SELECT * FROM templates ORDER BY position, id")


def get_template(id_: int) -> dict | None:
    rows = _query("SELECT * FROM templates WHERE id = ?", (id_,))
    return rows[0] if rows else None


def create_template(name: str, body: str) -> int:
    position = _query("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM templates")[0]["p"]
    return _execute(
        "INSERT INTO templates (name, body, position) VALUES (?, ?, ?)", (name, body, position)
    ).lastrowid


def update_template(id_: int, **fields) -> None:
    _update("templates", id_, fields)


def delete_template(id_: int) -> None:
    _execute("DELETE FROM templates WHERE id = ?", (id_,))


# ---------------------------------------------------------------- ajustes

def get_settings() -> dict:
    stored = {r["key"]: json.loads(r["value"]) for r in _query("SELECT key, value FROM settings")}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}


def save_settings(values: dict) -> dict:
    with _lock:
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                _conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )
        _conn.commit()
    return get_settings()


# ---------------------------------------------------------------- backup

def backup_zip() -> Path:
    """Cria um .zip temporário com uma cópia consistente do banco e todos os áudios."""
    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / DB_PATH.name
        with _lock, sqlite3.connect(copy) as dest:
            _conn.backup(dest)
        dest.close()
        with zipfile.ZipFile(zip_path, "w") as z:
            z.write(copy, DB_PATH.name, compress_type=zipfile.ZIP_DEFLATED)
            for audio in sorted(AUDIO_DIR.iterdir()):
                # Áudio já é comprimido: guardar sem recomprimir é bem mais rápido
                z.write(audio, f"{AUDIO_DIR.name}/{audio.name}", compress_type=zipfile.ZIP_STORED)
    return Path(zip_path)
