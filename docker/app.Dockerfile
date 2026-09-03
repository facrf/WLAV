FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system --gid 10001 wlav \
    && useradd --system --uid 10001 --gid wlav --home-dir /app wlav \
    && mkdir -p /var/whatsapp_media /var/wlav_imports /var/lib/wlav/secrets /imports /exports \
    && chown -R wlav:wlav /app /var/whatsapp_media /var/wlav_imports /var/lib/wlav /exports \
    && chmod 0777 /var/whatsapp_media /var/wlav_imports \
    && chmod 0700 /var/lib/wlav/secrets

COPY --chown=wlav:wlav pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md alembic.ini ./
COPY --chown=wlav:wlav backend ./backend
COPY --chown=wlav:wlav ingestor ./ingestor
COPY --chown=wlav:wlav frontend ./frontend
COPY --chown=root:root docker/wlav-wadecrypt /usr/local/bin/wlav-wadecrypt

RUN chmod 0555 /usr/local/bin/wlav-wadecrypt \
    && pip install --no-cache-dir .

USER wlav

EXPOSE 21001

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn backend.app.main:app --host 0.0.0.0 --port 21001 --proxy-headers --no-server-header"]
