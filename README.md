# WLAV — WhatsApp Local Archive & Viewer

O WLAV transforma backups do WhatsApp em um arquivo histórico pesquisável para
PCs e servidores domésticos. Ele guarda mensagens no PostgreSQL, organiza as
mídias em volume persistente e oferece uma interface semelhante ao WhatsApp Web.
Tudo funciona localmente em Docker, sem serviços externos.

## Escopo de segurança

O WLAV é deliberadamente um sistema **pessoal, sem login e voltado à rede
local**. Essa escolha simplifica o uso como arquivo histórico em computadores,
que normalmente têm muito mais armazenamento que smartphones.

- não encaminhe a porta `21001` no roteador;
- não publique o serviço diretamente na internet;
- permita a porta no firewall somente para a sua sub-rede confiável;
- mantenha `imports/`, `exports/`, `secrets/` e os volumes Docker fora de
  compartilhamentos públicos.

Não há autenticação na aplicação por decisão de projeto. `WLAV_BIND_ADDRESS`
pode ser definido como o IP LAN do servidor para restringir a interface usada.

## Recursos

- PostgreSQL 16 com `pg_trgm`, busca Full-Text Search em português e índices GIN;
- API FastAPI, paginação por cursor e mídia com HTTP `Range`;
- tema claro/escuro, busca global, infinite scroll, lightbox e players nativos;
- importação posterior de quantos backups forem necessários, pela UI ou CLI;
- UPSERT idempotente: mensagens existentes são atualizadas, não duplicadas;
- Android moderno/legado e suporte adaptativo ao `ChatStorage.sqlite` do iOS;
- thumbnails de fotos, stickers e vídeos com Pillow/FFmpeg;
- migrações de banco com Alembic executadas automaticamente;
- backup portátil com checksums SHA-256 e cifragem AES-256-GCM opcional;
- backup periódico cifrado, verificação automática e retenção configurável;
- licença MIT.

## 1. Instalação com Docker Compose

Requisitos: Docker Engine 24+ e Docker Compose v2.

```bash
cp .env.example .env
# Edite POSTGRES_PASSWORD e use a mesma senha, percent-encoded, em DATABASE_URL.
docker compose up -d --build
docker compose ps
```

Abra `http://IP-DESTA-MAQUINA:21001`. Para trocar a porta publicada, altere
`WLAV_PORT` no `.env`. Para escutar somente em um IP da LAN:

```env
WLAV_BIND_ADDRESS=192.168.1.10
WLAV_PORT=21001
```

Em Linux, ajuste `WLAV_UID` e `WLAV_GID` com os resultados de `id -u` e `id -g`.
Isso faz backups criados em `exports/` pertencerem ao usuário correto.

Na inicialização, `docker/init.sql` prepara o primeiro banco e o contêiner `app`
executa `alembic upgrade head`. Nas atualizações futuras, as migrações preservam
os dados já importados.

## 2. Importar pela interface

Clique no botão **↑ Importar backup** no cabeçalho lateral. É possível enviar:

- um `msgstore.db` Android descriptografado;
- um `ChatStorage.sqlite` iOS descriptografado;
- um ZIP, TAR ou TAR.GZ contendo o banco e as pastas de mídia;
- um backup portátil WLAV não cifrado.

Para incluir mídias, compacte uma estrutura semelhante a esta:

```text
backup.zip
└── WhatsApp/
    ├── msgstore.db
    └── Media/
        ├── WhatsApp Images/
        ├── WhatsApp Video/
        ├── WhatsApp Audio/
        ├── WhatsApp Voice Notes/
        └── WhatsApp Documents/
```

A tela mostra percentual do upload, mensagens processadas, mídias encontradas e
o histórico das importações. Fechar a janela não interrompe o processamento.
Depois, basta abrir novamente e enviar outro backup.

O limite padrão do upload é 100 GB e pode ser alterado com `UPLOAD_MAX_GB`. O
arquivo recebido é apagado do spool ao terminar; apenas dados normalizados e
mídias organizadas permanecem.

### Como múltiplos backups são combinados

Cada mensagem usa o identificador estável do WhatsApp como chave. Ao importar um
backup mais novo:

- mensagens novas são acrescentadas;
- mensagens já existentes são atualizadas;
- mídias previamente encontradas são preservadas se o novo pacote não as tiver;
- arquivos idênticos não são copiados novamente;
- nomes iguais com conteúdos diferentes recebem um sufixo estável.

Assim, backups mensais, trocas de telefone ou cópias parciais podem ser
adicionados ao mesmo arquivo histórico.

## 3. Importar pela CLI

Coloque o banco em `imports/` e as mídias, por exemplo, em `imports/Media/`.
Valide sem gravar:

```bash
docker compose run --rm ingestor ingest \
  --sqlite /imports/msgstore.db \
  --media-dir /imports/Media \
  --dry-run
```

Importe de fato:

```bash
docker compose run --rm ingestor ingest \
  --sqlite /imports/msgstore.db \
  --media-dir /imports/Media
```

Para iOS, substitua o caminho por `/imports/ChatStorage.sqlite`. O parser trata a
época `NSDate`, sessões, remetentes, mídias e respostas citadas quando as colunas
estão presentes. Como o schema interno varia entre versões do WhatsApp, use
sempre `--dry-run` primeiro; schemas desconhecidos são recusados sem gravar.

As mídias ficam em `{chat_jid}/{ano_mes}/{nome}`. Thumbnails são derivados dentro
de `.thumbs/`. Para gerar previews de mídias importadas por versões anteriores:

```bash
docker compose run --rm ingestor thumbnails
```

## 4. Backup manual, cifragem e integridade

Crie uma senha forte em arquivo ignorado pelo Git:

```bash
openssl rand -base64 48 > secrets/backup-password.txt
chmod 600 secrets/backup-password.txt
```

Backup portátil cifrado:

```bash
docker compose run --rm ingestor export \
  --output /exports/wlav-2026-09-03.wlavenc \
  --encrypt \
  --password-file /run/secrets/backup-password.txt
```

O diretório `secrets/` já é montado como somente leitura no contêiner de
ferramentas. Também é possível indicar outro arquivo ou a variável
`WLAV_BACKUP_PASSWORD_FILE`:

```bash
docker compose run --rm \
  -e WLAV_BACKUP_PASSWORD_FILE=/run/secrets/backup-password.txt \
  ingestor export --output /exports/wlav-backup.wlavenc --encrypt
```

Verifique e restaure:

```bash
docker compose run --rm \
  ingestor verify --archive /exports/wlav-backup.wlavenc \
  --password-file /run/secrets/backup-password.txt

docker compose run --rm \
  ingestor restore --archive /exports/wlav-backup.wlavenc \
  --password-file /run/secrets/backup-password.txt
```

A cifragem usa AES-256-GCM e uma chave derivada por Scrypt. Antes de restaurar, o
WLAV valida a autenticação criptográfica, caminhos do arquivo e SHA-256 de cada
JSONL, mídia e thumbnail. A senha não é armazenada no banco nem no backup.

Para exportar sem cifragem, omita `--encrypt` e use a extensão `.tar.gz`.

## 5. Backup automático e retenção

Depois de criar `secrets/backup-password.txt`, habilite o perfil de backup:

```bash
docker compose --profile backup up -d backup
docker compose logs -f backup
```

O primeiro backup é feito ao iniciar. Os seguintes obedecem:

```env
BACKUP_INTERVAL_HOURS=24
BACKUP_RETENTION_DAYS=30
```

Cada ciclo exporta, cifra, relê, autentica e confere todos os checksums. Somente
arquivos automáticos `wlav-auto-*` mais antigos que a retenção são removidos;
backups manuais nunca entram nessa limpeza.

> Um backup no mesmo disco protege contra erro lógico, mas não contra falha do
> disco. Sincronize `exports/` para outro PC, NAS ou mídia externa cifrada.

## 6. Instalação pelo Portainer

No Portainer, escolha **Stacks → Add stack → Repository**, informe o repositório
Git deste projeto e use `docker-compose.yml` como caminho do Compose. Configure
as variáveis de ambiente na própria Stack. O modo Repository é necessário porque
a imagem é construída a partir do código-fonte e do `docker/app.Dockerfile`.

Para uma stack dedicada, o YAML abaixo pode ser salvo no repositório como
`portainer-stack.yml`. Antes de ativar `backup`, crie no host
`/opt/wlav/secrets/backup-password.txt` e proteja-o com permissão `600`.

```yaml
name: wlav

services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-wlav}
      POSTGRES_USER: ${POSTGRES_USER:-wlav}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      TZ: America/Sao_Paulo
    volumes:
      - wlav_postgres:/var/lib/postgresql/data
      - ./docker/init.sql:/docker-entrypoint-initdb.d/10-wlav.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 12
    networks: [wlav_internal]

  app:
    build:
      context: .
      dockerfile: docker/app.Dockerfile
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    environment:
      DATABASE_URL: ${DATABASE_URL}
      MEDIA_ROOT: /var/whatsapp_media
      IMPORT_ROOT: /var/wlav_imports
      TMPDIR: /var/wlav_imports
      UPLOAD_MAX_GB: ${UPLOAD_MAX_GB:-100}
      THUMBNAIL_MAX_SIZE: ${THUMBNAIL_MAX_SIZE:-480}
      TZ: America/Sao_Paulo
    ports:
      - "${WLAV_BIND_ADDRESS:-0.0.0.0}:${WLAV_PORT:-21001}:21001"
    volumes:
      - wlav_media:/var/whatsapp_media
      - wlav_imports:/var/wlav_imports
    read_only: true
    tmpfs: [/tmp:size=64m]
    security_opt: [no-new-privileges:true]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:21001/api/health', timeout=3)"]
      interval: 10s
      timeout: 5s
      retries: 6
    networks: [wlav_internal]

  backup:
    build:
      context: .
      dockerfile: docker/app.Dockerfile
    restart: unless-stopped
    depends_on:
      app:
        condition: service_healthy
    environment:
      DATABASE_URL: ${DATABASE_URL}
      MEDIA_ROOT: /var/whatsapp_media
      WLAV_BACKUP_PASSWORD_FILE: /run/secrets/backup-password.txt
      TZ: America/Sao_Paulo
    entrypoint: ["python", "-m", "ingestor.main"]
    command: ["backup-loop", "--output-dir", "/exports", "--interval-hours", "${BACKUP_INTERVAL_HOURS:-24}", "--retention-days", "${BACKUP_RETENTION_DAYS:-30}", "--encrypt"]
    user: "${WLAV_UID:-1000}:${WLAV_GID:-1000}"
    volumes:
      - wlav_media:/var/whatsapp_media:ro
      - /opt/wlav/exports:/exports
      - /opt/wlav/secrets:/run/secrets:ro
    security_opt: [no-new-privileges:true]
    networks: [wlav_internal]

volumes:
  wlav_postgres:
  wlav_media:
  wlav_imports:

networks:
  wlav_internal:
    driver: bridge
```

Variáveis mínimas no Portainer:

```env
POSTGRES_PASSWORD=uma-senha-longa
DATABASE_URL=postgresql+psycopg://wlav:uma-senha-longa@db:5432/wlav
WLAV_PORT=21001
WLAV_BIND_ADDRESS=0.0.0.0
WLAV_UID=1000
WLAV_GID=1000
```

Se a senha contiver `@`, `:`, `/`, `?` ou `#`, aplique percent-encoding somente
na parte da senha em `DATABASE_URL`.

## API

| Rota | Uso |
|---|---|
| `GET /api/health` | Estado da aplicação e do banco |
| `GET /api/chats?q=&is_group=&limit=&offset=` | Lista e filtro de conversas |
| `GET /api/chats/{jid}/messages?before=&limit=` | Histórico por cursor |
| `GET /api/chats/{jid}/messages?around={id}` | Janela ao redor de uma mensagem |
| `GET /api/search?q=&page=&limit=&chat_jid=` | Busca FTS global |
| `POST /api/imports` | Upload de um novo backup |
| `GET /api/imports` | Histórico de importações |
| `GET /api/imports/{id}` | Progresso de uma importação |
| `GET /media/{caminho}` | Mídia local com suporte a `Range` |

Documentação interativa: `http://localhost:21001/api/docs`.

## Atualizações e migrações

Atualize o código e reconstrua:

```bash
docker compose up -d --build
docker compose logs app
```

O startup executa as migrações automaticamente. Para consultar ou aplicar
manualmente:

```bash
docker compose exec app alembic current
docker compose exec app alembic upgrade head
```

Nunca execute downgrade sem um backup íntegro recente.

## Operação e diagnóstico

```bash
docker compose logs -f app
docker compose logs -f db
docker compose exec db pg_isready -U wlav -d wlav
docker compose down                 # preserva todos os volumes
```

`docker compose down -v` apaga definitivamente banco, mídias e spool. Ele não é
um comando normal de manutenção.

## Desenvolvimento

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest
alembic upgrade head
uvicorn backend.app.main:app --reload --port 21001
```

## Estrutura

```text
backend/app/          API, modelos e trabalhos de importação
backend/migrations/   revisões Alembic
frontend/             interface estática
ingestor/             Android/iOS, mídia, cifragem e backups
docker/               imagem e inicialização PostgreSQL
tests/                testes automatizados
imports/              entrada CLI ignorada pelo Git
exports/              backups ignorados pelo Git
secrets/              senhas locais ignoradas pelo Git
```

## Recomendações adicionais

- monitore a saúde do disco com SMART e mantenha espaço livre para upload,
  extração e backup temporário;
- use UPS/No-break no PC ou NAS que hospeda o PostgreSQL;
- mantenha ao menos uma cópia cifrada fora da máquina principal;
- para acervos muito grandes, considere SSD para o PostgreSQL e HDD/NAS para uma
  segunda cópia dos exports;
- teste uma restauração completa periodicamente — um arquivo existir não garante
  que o procedimento de recuperação foi ensaiado.

## Licença

[MIT](LICENSE).
