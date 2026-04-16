"""
config/celery.py
Configuração do Celery para o projeto Isoscape.
"""

import os
from celery import Celery

# Define o settings padrão para o Celery
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("isoscape")

# Lê as configurações do Django com prefixo CELERY_
app.config_from_object("django.conf:settings", namespace="CELERY")

# Descobre tasks automaticamente em todos os apps instalados
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Task de teste — confirma que o worker está funcionando."""
    print(f"Request: {self.request!r}")