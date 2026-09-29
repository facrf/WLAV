# WLAV — WhatsApp Local Archive Vault

O WLAV transforma backups locais do WhatsApp Android em um arquivo histórico
pesquisável para PCs e servidores domésticos. Ele abre `msgstore.db.crypt15`
com a chave de 64 caracteres fornecida pelo proprietário, guarda mensagens no
PostgreSQL e organiza mídias em volume persistente. Tudo funciona localmente em
Docker, sem Google Drive nem serviços externos.

O WLAV é um projeto independente e não é afiliado, aprovado ou mantido pelo
WhatsApp ou pela Meta. Os formatos `.crypt*` não constituem uma API pública e
podem mudar.

## Escopo de segurança

O WLAV é deliberadamente um sistema **pessoal, sem login e voltado à rede
local**. Essa escolha simplifica o uso como arquivo histórico em computadores,
que normalmente têm muito mais armazenamento que smartphones.

- não encaminhe a porta `21001` no roteador;
- não publique o serviço diretamente na internet;
- permita a porta no firewall somente para a sua sub-rede confiável;
- mantenha `imports/`, `exports/`, `secrets/` e os volumes Docker fora de
  compartilhamentos públicos.
- use criptografia de disco no host; a chave salva protege dados em repouso
  somente enquanto o volume LUKS estiver fechado.

Não há autenticação na aplicação por decisão de projeto. `WLAV_BIND_ADDRESS`
pode ser definido como o IP LAN do servidor para restringir a interface usada.
Enquanto o serviço estiver ligado, qualquer pessoa com acesso à interface poderá
usar a chave salva para importar um backup, embora a API nunca revele seu valor.

## Recursos

- PostgreSQL 16 com `pg_trgm`, busca Full-Text Search em português e índices GIN;
- API FastAPI, paginação por cursor e mídia com HTTP `Range`;
- tema claro/escuro, busca global, infinite scroll, lightbox e players nativos;
- importação posterior de quantos backups forem necessários, pela UI ou CLI;
- abertura local de `msgstore.db.crypt15` com chave persistente de 64 caracteres;
- importação de conversas exportadas oficialmente em TXT/ZIP, inclusive com mídia;
- UPSERT idempotente: mensagens existentes são atualizadas, não duplicadas;
- Android moderno/legado e suporte adaptativo ao `ChatStorage.sqlite` do iOS;
- thumbnails de fotos, stickers e vídeos com Pillow/FFmpeg;
- migrações de banco com Alembic executadas automaticamente;
- backup portátil com checksums SHA-256 e cifragem AES-256-GCM opcional;
- backup periódico cifrado, verificação automática e retenção configurável;
- cabeçalhos de segurança com CSP estrito, já que a aplicação não tem login;
- importações em fila, uma de cada vez, com limpeza do spool no arranque;
- núcleo próprio sob licença MIT; compatibilidade `.crypt*` isolada em componente
  GPL-3.0-or-later, documentado ao fim deste arquivo.

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

Na inicialização, `docker/init.sql` instala apenas a extensão `pg_trgm` e o
contêiner `app` executa `alembic upgrade head`. A migração do Alembic é a **única
fonte de verdade** do schema: manter o DDL duplicado em `init.sql` fazia o banco
divergir da migração em silêncio. Nas atualizações futuras, as migrações
preservam os dados já importados.

### Usar a imagem pronta do GHCR

A imagem multi-arquitetura (`linux/amd64` e `linux/arm64`) é publicada em
`ghcr.io/facrf/wlav`. O Compose mantém a configuração de build local, portanto
o comando recomendado para desenvolvimento continua funcionando:

```bash
docker compose up -d --build
```

Para instalar ou atualizar sem compilar localmente, defina a tag desejada em
`.env` e solicite explicitamente a imagem pronta:

```env
WLAV_IMAGE=ghcr.io/facrf/wlav:latest
```

```bash
docker compose pull
docker compose up -d --no-build
```

Além de `latest`, cada publicação gera a tag `main`, uma tag curta baseada no
commit (`sha-...`) e, para tags Git como `v1.2.3`, as tags `1.2.3` e `1.2`.

## 2. Importar pela interface

Clique no botão **↑ Importar backup** no cabeçalho lateral. É possível enviar:

- um `msgstore.db.crypt15` Android e a pasta `Media`;
- um `.txt` ou ZIP criado por **Exportar conversa** no WhatsApp;
- um `msgstore.db` Android descriptografado;
- um `ChatStorage.sqlite` iOS descriptografado;
- um ZIP, TAR ou TAR.GZ contendo o banco e as pastas de mídia;
- um backup portátil WLAV não cifrado.

### Backup Android criptografado

No WhatsApp Android, abra **Configurações → Conversas → Backup de conversas →
Backup criptografado de ponta a ponta** e escolha a opção de chave com 64
caracteres. Guarde essa chave: ela não pode ser recuperada pelo WLAV.

Na tela de importação:

1. cole e salve a chave no cofre local;
2. copie do telefone a pasta
   `Android/media/com.whatsapp/WhatsApp` ou pelo menos `Databases` e `Media`;
3. use **Selecionar a pasta WhatsApp**, ou compacte essa estrutura em ZIP;
4. o WLAV escolhe primeiro `msgstore.db.crypt15`, descriptografa em área
   temporária, valida o SQLite e executa a importação idempotente;
5. o banco aberto é removido ao final, inclusive quando há falha.

A chave fica no volume Docker `key_store`, em
`/var/lib/wlav/secrets/whatsapp.key`, com modo `0600`. A API informa apenas
uma impressão SHA-256 curta para identificação; ela não retorna o segredo. A
chave não entra nos backups portáteis do WLAV e deve ter uma cópia separada e
segura. `docker compose down -v` apaga também esse volume.

O suporte principal é `.crypt15` com chave hexadecimal de 64 caracteres.
Backups `.crypt12` e `.crypt14` antigos podem exigir o arquivo de chave
privado da instalação antiga e, portanto, não são garantidos pela chave exibida
nas configurações atuais.

### Exportação oficial de uma conversa

No celular, abra a conversa e use **Mais opções → Mais → Exportar conversa**.
Escolha incluir ou não as mídias e salve o TXT/ZIP no computador. Na tela do
WLAV, informe opcionalmente o seu nome exatamente como aparece no TXT; isso
permite diferenciar mensagens enviadas e recebidas. Se o nome não for informado,
o WLAV tenta inferi-lo em conversas com duas pessoas.

O TXT não contém JIDs, confirmações de entrega, respostas citadas nem todos os
metadados internos. O WLAV não inventa esses dados. Datas sem fuso são
interpretadas usando `WHATSAPP_EXPORT_TIMEZONE` (padrão
`America/Sao_Paulo`) e gravadas em UTC. Em exportações norte-americanas, escolha
**mês/dia/ano** na tela; o modo automático usa dia/mês como padrão quando a data
é ambígua.

Reimportar a mesma exportação é seguro: o identificador estável considera
conversa, horário, remetente, conteúdo, mídia e a ocorrência de mensagens
idênticas.

### Pasta copiada do telefone

A tela também permite selecionar uma pasta `WhatsApp` completa. O navegador
envia os arquivos mantendo os caminhos relativos e o WLAV procura bancos,
exportações textuais e mídias. Esse modo é útil para organizar a entrada, mas não
remove a criptografia do WhatsApp.

Uma pasta real costuma ter dezenas de milhares de arquivos, e o limite padrão de
1.000 do parser multipart do Starlette tornaria esse fluxo impossível. Por isso o
endpoint lê o formulário com `IMPORT_MAX_FILES`, cujo padrão é 200.000 arquivos.
Reduza esse valor se preferir um teto mais rígido:

```env
IMPORT_MAX_FILES=20000
```

Cada importação é executada por vez, na ordem em que foi enfileirada. Duas
importações simultâneas duplicariam CPU, memória e I/O de disco sem ganho, já que
o UPSERT torna reimportar seguro.

Se a pasta contiver um `msgstore*.db.crypt15`, a chave salva será utilizada
automaticamente. O WLAV não extrai chaves do aplicativo, não contorna permissões
do Android e não envia dados a serviços externos.

### Banco SQLite com mídia

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
mídias organizadas permanecem. No arranque, o contêiner descarta os diretórios de
spool que não correspondem a uma importação enfileirada ou em andamento: um
processo morto no meio de um envio deixaria o diretório ocupando o volume para
sempre.

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

O comando distingue o que foi gerado, o que não se aplica (a mídia sumiu do
volume) e o que **falhou**, e sai com código diferente de zero se houve falhas.
Antes, uma `PermissionError` ao gravar dentro de um diretório criado pela
aplicação era contada como "ignorado" e passava despercebida. Os diretórios e
arquivos de mídia são criados com permissão de escrita para qualquer uid
(`0777`/`0666`), porque o volume é compartilhado entre a aplicação (uid 10001) e
as ferramentas de linha de comando (`WLAV_UID`).

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

### Formato do backup portátil

O formato atual é a **versão 3**, que grava `manifest.json` e `checksums.json` no
*início* do tar. Nas versões 1 e 2 esses dois arquivos ficavam no fim, e como o
gzip não permite saltos baratos, `verify` precisava de duas varreduras e `restore`
de quatro. Mover os metadados para o começo reduziu a restauração para duas
varreduras sem abrir mão da garantia: nada é gravado no banco nem no volume antes
de o arquivo inteiro passar pelos checksums.

Backups v1 e v2 continuam legíveis. A v1 nunca gravou hashes, e por isso não é
possível garantir a integridade do conteúdo dela — a verificação se limita a
confirmar que o tar é legível.

Um arquivo com um membro a mais, a menos, renomeado ou adulterado é recusado
antes de qualquer escrita, tanto por incompatibilidade com `checksums.json`
quanto por nome de caminho inseguro.

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
as variáveis de ambiente na própria Stack. Por padrão, a Stack pode baixar
`ghcr.io/facrf/wlav:latest`; ainda é possível selecionar o build a partir do
código-fonte em ambientes de desenvolvimento.

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
    image: ${WLAV_IMAGE:-ghcr.io/facrf/wlav:latest}
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    environment:
      DATABASE_URL: ${DATABASE_URL}
      MEDIA_ROOT: /var/whatsapp_media
      IMPORT_ROOT: /var/wlav_imports
      WHATSAPP_KEY_PATH: /var/lib/wlav/secrets/whatsapp.key
      TMPDIR: /var/wlav_imports
      UPLOAD_MAX_GB: ${UPLOAD_MAX_GB:-100}
      IMPORT_MAX_FILES: ${IMPORT_MAX_FILES:-200000}
      THUMBNAIL_MAX_SIZE: ${THUMBNAIL_MAX_SIZE:-480}
      TZ: America/Sao_Paulo
    ports:
      - "${WLAV_BIND_ADDRESS:-0.0.0.0}:${WLAV_PORT:-21001}:21001"
    volumes:
      - wlav_media:/var/whatsapp_media
      - wlav_imports:/var/wlav_imports
      - wlav_keys:/var/lib/wlav/secrets
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
    image: ${WLAV_IMAGE:-ghcr.io/facrf/wlav:latest}
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
  wlav_keys:

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
WLAV_IMAGE=ghcr.io/facrf/wlav:latest
```

Se a senha contiver `@`, `:`, `/`, `?` ou `#`, aplique percent-encoding somente
na parte da senha em `DATABASE_URL`.

`IMPORT_MAX_FILES` e `IMPORT_MAX_FIELDS` só precisam ser declarados se você quiser
um teto diferente do padrão do código.

## API

| Rota | Uso |
|---|---|
| `GET /api/health` | Estado da aplicação e do banco |
| `GET /api/chats?q=&is_group=&limit=&offset=` | Lista e filtro de conversas |
| `GET /api/chats/{jid}/messages?before=&limit=` | Histórico por cursor |
| `GET /api/chats/{jid}/messages?around={id}` | Janela ao redor de uma mensagem |
| `GET /api/search?q=&page=&limit=&chat_jid=` | Busca FTS global |
| `POST /api/imports` | Multipart com `file` (um ou vários), `owner_name` opcional e `date_order` (`auto`, `dmy` ou `mdy`) |
| `GET /api/imports` | Histórico de importações |
| `GET /api/imports/{id}` | Progresso de uma importação |
| `GET /api/settings/whatsapp-key` | Estado e impressão da chave, nunca o valor |
| `PUT /api/settings/whatsapp-key` | Salva ou substitui a chave de 64 caracteres |
| `DELETE /api/settings/whatsapp-key` | Remove a chave salva |
| `GET /media/{caminho}` | Mídia local com suporte a `Range` |

`POST /api/imports` responde:

| Código | Quando |
|---|---|
| `202` | Upload recebido; a importação começa na fila |
| `400` | multipart malformado, nome de arquivo inseguro ou caminho duplicado |
| `413` | corpo acima de `UPLOAD_MAX_GB` |
| `422` | nenhum arquivo enviado, ou `date_order` inválido |

O corpo é lido manualmente para poder elevar o limite de arquivos do multipart, o
que significa que o FastAPI não o infere da assinatura da rota. O esquema
`requestBody` está declarado explicitamente para o `/api/docs` continuar correto.

Documentação interativa: `http://localhost:21001/api/docs`.

## Cabeçalhos de segurança

Todas as respostas, exceto o `/api/docs` e o `/api/openapi.json`, recebem:

```text
Content-Security-Policy: default-src 'self'; base-uri 'none'; object-src 'none';
  frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self';
  img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Permissions-Policy: geolocation=(), microphone=(), camera=()
```

Como a aplicação não tem autenticação, um XSS daria acesso total ao arquivo
histórico. O front-end não usa estilo nem script inline, o que permite um CSP
estrito, sem `unsafe-inline`. O Swagger UI é a exceção: ele é servido de um CDN e
precisa de script inline.

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

Para apontar as migrações a outro banco sem alterar o ambiente do processo:

```bash
docker compose exec app alembic -x database_url=postgresql+psycopg://... upgrade head
```

A revisão `20260929_0002` acrescenta o índice `ix_chats_last_message_time`, usado
pela ordenação de `GET /api/chats`. Sem ele, cada página paginada pagava um sort
completo da tabela de conversas.

Os índices também estão declarados no metadata dos models, e `tests/test_schema.py`
compara o schema criado pelas migrações com o que os models declaram. É o que
impede um índice de ser removido dos models sem ninguém perceber, já que o
`create_all()` deixaria de criá-lo e o banco continuaria funcionando.

Nunca execute downgrade sem um backup íntegro recente.

## Publicação da imagem e proteção dos espelhos

O workflow [`.github/workflows/publish-ghcr.yml`](.github/workflows/publish-ghcr.yml)
é o único responsável por construir imagens do WLAV. Ele roda em pushes para
`main`, em tags iniciadas por `v` e manualmente pelo GitHub Actions. Antes da
publicação, executa Ruff e Pytest; depois publica somente em
`ghcr.io/facrf/wlav`, com cache, SBOM e proveniência OCI.

O token usado pelo push mirror para o GitHub precisa ter acesso de escrita ao
conteúdo e aos workflows do repositório. Em um PAT clássico, inclua o escopo
`workflow`; em um token fine-grained, limite-o ao repositório `facrf/WLAV` e
conceda **Contents: Read and write** e **Workflows: Read and write**. Sem essa
permissão, o GitHub pode recusar a inclusão ou atualização deste workflow.

Há três camadas para impedir builds nos espelhos:

1. o job exige simultaneamente `github.server_url == 'https://github.com'` e
   `github.repository == 'facrf/WLAV'`;
2. `.gitea/workflows` e `.forgejo/workflows` existem sem nenhum YAML, evitando o
   fallback dessas plataformas para `.github/workflows`;
3. no Gitea e no Forgejo, mantenha desmarcada a unidade **Actions** em
   **Settings → Units → Overview** para cada repositório espelho. Se a instância
   inteira não usa Actions, o administrador também pode definir
   `[actions] ENABLED = false` no `app.ini`.

Não cadastre tokens do GHCR no Gitea ou no Forgejo. O GitHub Actions usa apenas o
`GITHUB_TOKEN` efêmero, limitado pelas permissões declaradas no workflow. Na
primeira publicação, o pacote do GHCR nasce privado; para permitir pulls sem
login, abra o pacote `wlav` no GitHub, acesse **Package settings → Change
visibility** e torne-o público. Essa alteração de visibilidade é irreversível.

## Operação e diagnóstico

```bash
docker compose logs -f app
docker compose logs -f db
docker compose exec db pg_isready -U wlav -d wlav
docker compose down                 # preserva todos os volumes
```

`docker compose down -v` apaga definitivamente banco, mídias, spool e a chave
salva do WhatsApp. Ele não é um comando normal de manutenção.

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

### Testes

`pytest` sozinho roda a suíte que não precisa de banco. Com
`WLAV_TEST_DATABASE_URL` apontando para um PostgreSQL descartável, os testes
restantes também são executados; sem a variável eles são pulados, e não falham.

```bash
docker run -d --name wlav-testdb -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
  postgres:16-alpine

WLAV_TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@127.0.0.1:55432/wlav_test' \
  pytest
```

A fixture cria o banco se ele não existir, recria o schema `public` e aplica as
migrações do Alembic de verdade, e não `create_all`. É por isso que o schema
resultante é comparável com o que os models declaram.

Com o banco disponível, a suíte cobre o que antes só tinha dublês de sessão: a
paginação por cursor, a janela `around`, a busca FTS e a ordenação de conversas,
além da exportação, verificação e restauração do backup portátil, que não tinham
nenhum teste. No CI o serviço `db` do workflow faz esse papel, então a publicação
da imagem só acontece com a suíte completa verde.

## Estrutura

```text
backend/app/          API, modelos e trabalhos de importação
backend/migrations/   revisões Alembic
frontend/             interface estática
ingestor/             Android/iOS, mídia, cifragem e backups
docker/               imagem e inicialização PostgreSQL
.github/workflows/     testes e publicação exclusiva no GHCR
.gitea/workflows/      bloqueio do fallback no espelho Gitea
.forgejo/workflows/    bloqueio do fallback no espelho Forgejo
tests/                testes automatizados
imports/              entrada CLI ignorada pelo Git
exports/              backups ignorados pelo Git
secrets/              senhas locais ignoradas pelo Git
```

### Componente de compatibilidade com backups cifrados

A descriptografia é executada em processo separado pelo
[wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools), GPL-3.0-or-later.
O restante do WLAV continua sob MIT. A versão exata está fixada em
`pyproject.toml`; consulte [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

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
