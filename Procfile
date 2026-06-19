web: python backend/manage.py collectstatic --no-input && python backend/manage.py migrate && gunicorn --chdir backend config.wsgi:application
