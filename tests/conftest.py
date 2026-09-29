"""Fixtures compartilhadas.

`WLAV_TEST_DATABASE_URL` aponta para um PostgreSQL descartável. Quando a variável
não existe, os testes que dependem do banco são ignorados em vez de falhar, para
que `pytest` continue utilizável numa máquina sem Docker. No CI a variável é
definida pelo serviço `db` do workflow.

A URL precisa existir ANTES de qualquer importação de `backend.app`, porque
`backend.app.database` cria o engine no import. Por isso a configuração abaixo é
feita no escopo do módulo do conftest, que o pytest carrega antes dos testes.
"""

import os
from argparse import Namespace
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(PROJECT_ROOT))

from tests.support import TEST_DATABASE_URL  # noqa: E402  (depende do sys.path acima)

if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ.setdefault("MEDIA_ROOT", "/tmp/wlav-test-media")
    os.environ.setdefault("IMPORT_ROOT", "/tmp/wlav-test-imports")
    os.environ.setdefault("WHATSAPP_KEY_PATH", "/tmp/wlav-test-secrets/whatsapp.key")


@pytest.fixture(scope="session")
def database_url() -> str:
    """Garante que o banco descartável exista e devolve a URL da aplicação."""
    if not TEST_DATABASE_URL:
        pytest.skip("WLAV_TEST_DATABASE_URL não definida")
    import psycopg
    from sqlalchemy.engine import make_url

    url = make_url(TEST_DATABASE_URL)
    # `psycopg.connect` não entende o sufixo de driver do SQLAlchemy.
    common = {
        "host": url.host,
        "port": url.port,
        "user": url.username,
        "password": url.password,
        "connect_timeout": 5,
    }
    try:
        with psycopg.connect(dbname=url.database, autocommit=True, **common):
            pass
    except psycopg.OperationalError:
        # O banco ainda não existe: cria pelo banco de manutenção do mesmo servidor.
        with psycopg.connect(dbname="postgres", autocommit=True, **common) as connection:
            connection.execute(f'CREATE DATABASE "{url.database}"')
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_database_url(database_url: str) -> str:
    """Aplica as migrações do Alembic em um schema público limpo.

    Rodar as migrações de verdade (e não `create_all`) é o que permite
    `test_schema.py` detectar divergência entre o DDL das migrações e os models.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "backend" / "migrations"))
    # `env.py` lê a URL de `-x database_url=...`; o Alembic 1.19 só expõe esse
    # canal via `cmd_opts`, então é preciso montá-lo à mão.
    config.cmd_opts = Namespace(x=[f"database_url={database_url}"])
    command.upgrade(config, "head")
    return database_url


@pytest.fixture
def session(migrated_database_url: str):
    """Sessão limpa por teste, com as tabelas truncadas."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    engine = create_engine(migrated_database_url, poolclass=None)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE messages, chats, import_jobs CASCADE"))
    with Session(engine) as db_session:
        yield db_session
    engine.dispose()


@pytest.fixture
def client(migrated_database_url: str, monkeypatch, tmp_path):
    """TestClient com lifespan real (banco de verdade, mídia em disco temporário)."""
    from fastapi.testclient import TestClient

    from backend.app import main

    media_root = tmp_path / "media"
    import_root = tmp_path / "imports"
    key_path = tmp_path / "secrets" / "whatsapp.key"
    for path in (media_root, import_root, key_path.parent):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main.settings, "media_root", media_root)
    monkeypatch.setattr(main.settings, "import_root", import_root)
    monkeypatch.setattr(main.settings, "whatsapp_key_path", key_path)
    monkeypatch.setattr(main, "whatsapp_key_store", main.WhatsAppKeyStore(key_path))
    with TestClient(main.app) as test_client:
        yield test_client
