"""
config/settings/base.py
Configurações compartilhadas entre todos os ambientes.
"""

import os
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

# ── Diretórios base ────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path("/data")

# ── Segurança ──────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY")

# ── Apps instalados ────────────────────────────────────
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
]

LOCAL_APPS = [
    "apps.users",
    "apps.projects",
    "apps.datasets",
    "apps.shapefiles",
    "apps.rasters",
    "apps.jobs",
    "apps.isoscapes",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ── Middleware ─────────────────────────────────────────
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",        # deve vir antes de CommonMiddleware
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ── Banco de Dados — MariaDB ───────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("DB_NAME", "isoscape_db"),
        "USER": os.environ.get("DB_USER", "isoscape_user"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "db"),
        "PORT": os.environ.get("DB_PORT", "3306"),
        "OPTIONS": {
            "charset": "utf8mb4",
        },
    }
}

# ── Autenticação ───────────────────────────────────────
# AUTH_USER_MODEL = "users.User"   # modelo customizado (a criar)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── DRF + JWT ──────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
}

from datetime import timedelta

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
}

# ── Celery ─────────────────────────────────────────────
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "America/Sao_Paulo"

# ── Script Execution Engine ────────────────────────────
# Escolhe qual engine usar para executar scripts de processamento:
# - 'python': executa versões Python dos scripts (padrão)
# - 'r': executa versões R originais
EXECUTION_ENGINE = os.environ.get("EXECUTION_ENGINE", "python")

# Se True, tenta fallback para R se Python falhar
SCRIPT_ENGINE_FALLBACK = os.environ.get("SCRIPT_ENGINE_FALLBACK", "true").lower() == "true"

# ── Internacionalização ────────────────────────────────
LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

# ── Arquivos estáticos e de mídia ──────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "mediafiles"

# ── Caminhos de dados (rasters, shapefiles, isoscapes) ─
DATA_RASTERS_DIR    = DATA_DIR / "rasters"
DATA_SHAPEFILES_DIR = DATA_DIR / "shapefiles"
DATA_ISOSCAPES_DIR  = DATA_DIR / "isoscapes"
DATA_DATASETS_DIR   = DATA_DIR / "datasets"
DATA_WORLDCLIM_DIR  = DATA_DIR / "worldclim_cache"

# ── Default primary key ────────────────────────────────
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"