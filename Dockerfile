# Imagem de produção — mesma base do sistema interno da consultoria
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Sao_Paulo \
    FLASK_APP=wsgi.py

WORKDIR /srv/bangalo

# tzdata: fuso de São Paulo. postgresql-client: pg_dump usado por scripts/backup_db.sh.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Normaliza fins de linha (o repo é editado no Windows) e torna executável
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh \
    && sed -i 's/\r$//' scripts/*.sh && chmod +x scripts/*.sh

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
