FROM python:3.11-slim

# Variables de entorno del sistema
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Instalar dependencias del sistema (PostgreSQL client)
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el proyecto completo
COPY . .

# Exponer el puerto
EXPOSE 8000

# Comando de inicio: collectstatic + migrate + gunicorn
CMD python backend/manage.py collectstatic --no-input && \
    python backend/manage.py migrate && \
    gunicorn --chdir backend config.wsgi:application --bind 0.0.0.0:${PORT:-8000}
