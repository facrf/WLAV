FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system --gid 10001 wlav \
    && useradd --system --uid 10001 --gid wlav --home-dir /app wlav \
    && mkdir -p /var/whatsapp_media /var/wlav_imports /imports /exports \
    && chown -R wlav:wlav /app /var/whatsapp_media /var/wlav_imports /exports \
    && chmod 0777 /var/whatsapp_media /var/wlav_imports

COPY --chown=wlav:wlav pyproject.toml README.md LICENSE alembic.ini ./
COPY --chown=wlav:wlav backend ./backend
COPY --chown=wlav:wlav ingestor ./ingestor
COPY --chown=wlav:wlav frontend ./frontend

RUN pip install --no-cache-dir .

USER wlav

EXPOSE 21001

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn backend.app.main:app --host 0.0.0.0 --port 21001 --proxy-headers --no-server-header"]
