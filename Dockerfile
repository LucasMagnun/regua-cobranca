FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ src/

# Não roda como root
RUN useradd --create-home --uid 1000 regua
USER regua

# Executa uma vez e termina (é o que o Cloud Run Jobs espera)
ENTRYPOINT ["python", "-m", "regua.main"]
