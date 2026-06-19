#!/usr/bin/env bash
# exit on error
set -o errexit

# Install dependencies
pip install -r requirements.txt

# Collect static files (con variables de entorno por defecto para el build)
DJANGO_SETTINGS_MODULE=config.settings \
SECRET_KEY="django-insecure-build-key" \
EMAIL_HOST="smtp.gmail.com" \
EMAIL_PORT="587" \
EMAIL_HOST_USER="" \
EMAIL_HOST_PASSWORD="" \
EMAIL_USE_TLS="True" \
EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend" \
python backend/manage.py collectstatic --no-input
