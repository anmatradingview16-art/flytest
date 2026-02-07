# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY aruodas_clicker.py ./

# (Nebūtina, bet geriau saugumui) paleidžiam ne kaip root
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Fly 'internal_port' bus 8080, todėl čia ir bind'inam 8080
CMD ["gunicorn", "aruodas_clicker:app", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "90"]
