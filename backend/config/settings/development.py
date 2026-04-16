"""
config/settings/development.py
Configurações para ambiente local de desenvolvimento.
"""

from .base import *

# ── Debug ──────────────────────────────────────────────
DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0"]

# ── CORS — permite o React dev server (porta 5173) ─────
CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",   # Vite dev server
    "http://localhost:80",
]
CORS_ALLOW_CREDENTIALS = True

# ── Emails — imprime no terminal em dev ────────────────
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ── Django Debug Toolbar (opcional) ───────────────────
# INSTALLED_APPS += ["debug_toolbar"]
# MIDDLEWARE += ["debug_toolbar.middleware.DebugToolbarMiddleware"]
# INTERNAL_IPS = ["127.0.0.1"]