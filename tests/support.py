"""Marcadores e utilitários compartilhados pelos testes.

Fica em um módulo próprio (e não no `conftest.py`) para que os módulos de teste
consigam importar o marcador de forma explícita, sem depender do modo de
importação do pytest.
"""

import os

import pytest

TEST_DATABASE_URL = os.getenv("WLAV_TEST_DATABASE_URL", "").strip()

requires_database = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="defina WLAV_TEST_DATABASE_URL para rodar os testes que usam o PostgreSQL",
)
