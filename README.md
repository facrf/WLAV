# WLAV — WhatsApp Local Archive & Viewer

O WLAV importa um `msgstore.db` descriptografado para PostgreSQL e oferece um
arquivo web pesquisável, com conversas, respostas citadas, imagens, vídeos,
áudios Opus/Ogg e documentos. Todo o stack roda em Docker e não depende de
serviços externos.

> **Privacidade:** backups do WhatsApp contêm dados pessoais. Não publique a
> porta do WLAV na internet. Use-o somente em uma LAN confiável ou atrás de uma
> VPN/proxy com autenticação, e proteja também `imports/`, `exports/` e os volumes
> Docker.

## Recursos

- PostgreSQL 16, `pg_trgm`, índice GIN Full-Text Search em português e índice
  composto para paginação do histórico;
- API FastAPI documentada em `/api/docs`, paginação por cursor e streaming HTTP
  com suporte a `Range` para áudio/vídeo;
- interface responsiva inspirada no WhatsApp Web, tema claro/escuro, busca global,
  lightbox e carregamento progressivo;
- importador idempotente para os schemas Android moderno (`message/chat/jid`) e
  legado (`messages/chat_list`), sem carregar toda a tabela de mensagens na RAM;
- backup portátil `.tar.gz` com JSONL + mídias e restauração idempotente;
- licença MIT.

## 1. Início rápido

Requisitos: Docker Engine 24+ com Docker Compose v2.

```bash
cp .env.example .env
# Edite .env e use uma senha longa; DATABASE_URL deve conter a mesma senha.
docker compose up -d --build
docker compose ps
```

Abra `http://IP-DESTA-MAQUINA:21001`. Para trocar a porta publicada, defina, por
exemplo, `WLAV_PORT=8080` no `.env`.

Em Linux, `WLAV_UID` e `WLAV_GID` fazem o contêiner de importação criar os
backups como o seu usuário. Os valores padrão são `1000`; confira com `id -u` e
`id -g` se a sua conta usar outros números.

As tabelas e extensões são criadas pelo arquivo `docker/init.sql` na primeira
inicialização do volume PostgreSQL. O banco não é exposto na máquina hospedeira.

## 2. Preparar e importar um backup

O banco precisa estar **descriptografado**. O WLAV não contorna criptografia nem
extrai dados de aparelhos: obtenha os arquivos de um aparelho/backup que você tem
autorização para administrar.

Coloque os dados desta forma (o nome da pasta de mídias pode variar):

```text
imports/
├── msgstore.db
└── Media/
    ├── WhatsApp Images/
    ├── WhatsApp Video/
    ├── WhatsApp Audio/
    ├── WhatsApp Voice Notes/
    └── WhatsApp Documents/
```

Primeiro faça uma leitura de validação, sem gravar nada:

```bash
docker compose run --rm ingestor ingest \
  --sqlite /imports/msgstore.db \
  --media-dir /imports/Media \
  --dry-run
```

Depois importe:

```bash
docker compose run --rm ingestor ingest \
  --sqlite /imports/msgstore.db \
  --media-dir /imports/Media
```

Os arquivos encontrados são copiados para o volume no formato
`{chat_jid}/{ano_mes}/{nome_do_arquivo}`. Colisões recebem um sufixo estável. O
comando pode ser executado novamente: chats e mensagens usam `UPSERT`, e arquivos
idênticos não são duplicados. Reinicie somente se quiser renovar processos; a UI
vê os dados importados imediatamente.

```bash
docker compose restart app
```

### Compatibilidade do SQLite

O parser detecta automaticamente as duas famílias mais comuns de `msgstore.db`
do WhatsApp Android. Os nomes/colunas internos mudam entre versões; quando uma
variante não é reconhecida, o comando para com uma lista das tabelas encontradas,
sem alterar o PostgreSQL.

No iPhone, o banco nativo costuma se chamar `ChatStorage.sqlite` e usa um schema
Core Data diferente — apesar de algumas ferramentas chamarem todo backup de
“msgstore”. Esta versão não deve receber esse arquivo como se fosse Android.
Converta-o antes para um `msgstore.db` compatível ou para um arquivo portátil WLAV.
Essa separação evita importações silenciosamente incorretas.

## 3. Exportar e restaurar

Para criar um arquivo portátil que contém banco lógico e mídias:

```bash
docker compose run --rm ingestor export \
  --output /exports/wlav-2026-09-03.tar.gz
```

O resultado aparece em `exports/`. Para restaurar em uma instalação WLAV vazia
ou existente:

```bash
docker compose run --rm ingestor restore \
  --archive /exports/wlav-2026-09-03.tar.gz
```

A restauração valida versão, caminhos, entradas duplicadas e links antes de
copiar. Ela também é idempotente. Ainda assim, mantenha pelo menos outra cópia do
arquivo em armazenamento cifrado.

Para copiar os volumes Docker integralmente, também é possível usar `pg_dump` e
uma ferramenta de backup de volumes, mas o formato WLAV é mais simples para
migração entre máquinas.

## API

| Rota | Uso |
|---|---|
| `GET /api/health` | Estado da aplicação e do banco |
| `GET /api/chats?q=&is_group=&limit=&offset=` | Lista/filtro de conversas |
| `GET /api/chats/{jid}/messages?before=&limit=` | Histórico por cursor |
| `GET /api/chats/{jid}/messages?around={id}` | Janela ao redor de uma mensagem |
| `GET /api/search?q=&page=&limit=&chat_jid=` | Busca FTS global |
| `GET /media/{caminho}` | Mídia local com suporte a `Range` |

Exemplo:

```bash
curl --get 'http://localhost:21001/api/search' \
  --data-urlencode 'q=proposta aprovada'
```

## Operação e diagnóstico

```bash
docker compose logs -f app
docker compose logs -f db
docker compose exec db pg_isready -U wlav -d wlav
docker compose down                 # preserva os volumes
```

`docker compose down -v` apaga definitivamente banco e mídias e, por isso, não é
um comando normal de manutenção.

Se a senha for alterada depois que o volume do PostgreSQL já existe, a variável
não redefine automaticamente a senha interna. Ajuste-a no banco ou restaure um
backup em um volume novo. Senhas com caracteres reservados (`@`, `:`, `/`) devem
ser percent-encoded em `DATABASE_URL`.

## Desenvolvimento local

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest
uvicorn backend.app.main:app --reload --port 21001
```

Para rodar a API fora do Docker, configure `DATABASE_URL`, `MEDIA_ROOT` e tenha um
PostgreSQL inicializado com `docker/init.sql`.

## Estrutura

```text
backend/app/       API, modelos e configuração
frontend/          interface estática sem etapa de build
ingestor/          leitores SQLite, mídia e arquivo portátil
docker/            Dockerfile e inicialização PostgreSQL
tests/             testes do parser e armazenamento de mídia
imports/           entrada local ignorada pelo Git
exports/           backups gerados ignorados pelo Git
```

## Licença

[MIT](LICENSE).
