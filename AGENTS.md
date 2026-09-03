# AGENTS.md — WLAV

## Escopo

Estas instruções se aplicam somente a esta pasta (`WLAV/`) e a todas as suas
subpastas. Não altere arquivos fora deste diretório.

## Regras do projeto

- Preserve compatibilidade com `docker compose up -d --build`.
- Não versione bancos, backups, mídias, segredos ou dados pessoais de conversas.
- Mantenha caminhos de mídia relativos ao volume; valide qualquer caminho antes
  de servir, copiar ou extrair arquivos.
- Toda importação precisa ser idempotente, usando as chaves naturais existentes.
- Mudanças em endpoints devem ser refletidas no `README.md` e, quando aplicável,
  no front-end.
- Use timestamps com fuso (`TIMESTAMPTZ`) e normalize épocas do WhatsApp para UTC.
- Execute `pytest` antes de concluir mudanças em Python.
- Prefira dependências com licença permissiva e registre novas dependências em
  `pyproject.toml`.

