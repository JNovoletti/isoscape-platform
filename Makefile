# =============================================================================
# Makefile — Atalhos para comandos Docker frequentes
# =============================================================================
# Como usar: make <comando>
#   Ex: make up
#
# Requisito: GNU Make instalado (padrão no Linux/Mac; no Windows use WSL2)
# =============================================================================

COMPOSE = docker compose
BACKEND = db redis backend worker

.PHONY: help \
        up down build rebuild logs ps clean \
        migrate makemigrations shell superuser \
        worker-logs backend-logs \
        test-r test-python \
        rasters-r rasters-py iso-r iso-py

# -----------------------------------------------------------------------------
# Ajuda
# -----------------------------------------------------------------------------
help:
	@echo ""
	@echo "  isoscape-platform — Comandos Docker"
	@echo "  ──────────────────────────────────────────────────────"
	@echo "  Ambiente:"
	@echo "    make up              Sobe todos os serviços"
	@echo "    make down            Derruba todos os serviços"
	@echo "    make build           Build das imagens sem subir"
	@echo "    make rebuild         Build forçado (--no-cache) e sobe"
	@echo "    make logs            Logs de todos os serviços em tempo real"
	@echo "    make worker-logs     Logs apenas do worker Celery"
	@echo "    make backend-logs    Logs apenas do backend Django"
	@echo "    make ps              Lista containers rodando"
	@echo "    make clean           Remove containers e volumes (APAGA BANCO)"
	@echo ""
	@echo "  Django:"
	@echo "    make migrate         Aplica migrations"
	@echo "    make makemigrations  Gera novas migrations"
	@echo "    make shell           Shell interativo do Django (no worker)"
	@echo "    make superuser       Cria superusuário admin"
	@echo ""
	@echo "  Testes manuais (engine R):"
	@echo "    make rasters-r       Testa gen_rasters.R manualmente"
	@echo "    make iso-r           Testa run_isoscape.R manualmente"
	@echo ""
	@echo "  Testes manuais (engine Python):"
	@echo "    make rasters-py      Testa gen_rasters.py manualmente"
	@echo "    make iso-py          Testa run_isoscape.py manualmente"
	@echo ""
	@echo "  Diagnóstico:"
	@echo "    make check-tifs      Lista .tif gerados em /data/rasters"
	@echo "    make check-iso       Lista isoscapes gerados em /data/isoscapes"
	@echo "    make check-log-r     Exibe log do último gen_rasters R"
	@echo "    make check-log-py    Exibe log do último gen_rasters Python"
	@echo ""

# -----------------------------------------------------------------------------
# Ambiente
# -----------------------------------------------------------------------------
up:
	$(COMPOSE) up -d

up-back:
	$(COMPOSE) up -d $(BACKEND)

down:
	$(COMPOSE) down

build:
	$(COMPOSE) build

rebuild:
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

rebuild-back:
	$(COMPOSE) build --no-cache $(BACKEND)
	$(COMPOSE) up -d $(BACKEND)

logs:
	$(COMPOSE) logs -f

worker-logs:
	$(COMPOSE) logs -f worker

backend-logs:
	$(COMPOSE) logs -f backend

ps:
	$(COMPOSE) ps

# CUIDADO: apaga volumes (banco de dados incluído)
clean:
	$(COMPOSE) down -v
	@echo "Containers e volumes removidos."

# -----------------------------------------------------------------------------
# Django
# -----------------------------------------------------------------------------
migrate:
	$(COMPOSE) exec worker python manage.py migrate

makemigrations:
	$(COMPOSE) exec worker python manage.py makemigrations

shell:
	$(COMPOSE) exec worker python manage.py shell

superuser:
	$(COMPOSE) exec worker python manage.py createsuperuser

# -----------------------------------------------------------------------------
# Testes manuais — engine R
# -----------------------------------------------------------------------------
# Variáveis configuráveis via linha de comando:
#   make rasters-r SHAPEFILE=/data/shapefiles/outro.shp VARS=tmax RESOLUTION=5
SHAPEFILE   ?= /data/shapefiles/amazonia_legal.shp
VARS        ?= tavg
RESOLUTION  ?= 10
OUTPUT_DIR  ?= /tmp/debug_rasters
ISO_OUTPUT  ?= /tmp/debug_iso
DATASET     ?= /data/datasets/madeiras.csv
RASTER_DIR  ?= /data/rasters/1/amazonia_legal
RESPONSE    ?= d13C_wood
LAT         ?= latitude
LON         ?= longitude
UNCERTAINTY ?= quantile_rf

rasters-r:
	$(COMPOSE) exec worker Rscript r_scripts/gen_rasters.R \
		--job-id debug-r \
		--shapefile $(SHAPEFILE) \
		--output-dir $(OUTPUT_DIR)_r \
		--worldclim-dir /data/worldclim_cache \
		--variables $(VARS) \
		--resolution $(RESOLUTION) \
		--skip-existing FALSE

iso-r:
	$(COMPOSE) exec worker Rscript r_scripts/run_isoscape.R \
		--job-id debug-r \
		--dataset-path $(DATASET) \
		--raster-dir $(RASTER_DIR) \
		--output-dir $(ISO_OUTPUT)_r \
		--response-col $(RESPONSE) \
		--lat-col $(LAT) \
		--lon-col $(LON) \
		--uncertainty $(UNCERTAINTY) \
		--resolution $(RESOLUTION)

# -----------------------------------------------------------------------------
# Testes manuais — engine Python
# -----------------------------------------------------------------------------
rasters-py:
	$(COMPOSE) exec worker python python_scripts/gen_rasters.py \
		--job-id debug-py \
		--shapefile $(SHAPEFILE) \
		--output-dir $(OUTPUT_DIR)_py \
		--worldclim-dir /data/worldclim_cache \
		--variables $(VARS) \
		--resolution $(RESOLUTION) \
		--skip-existing false

iso-py:
	$(COMPOSE) exec worker python python_scripts/run_isoscape.py \
		--job-id debug-py \
		--dataset-path $(DATASET) \
		--raster-dir $(RASTER_DIR) \
		--output-dir $(ISO_OUTPUT)_py \
		--response-col $(RESPONSE) \
		--lat-col $(LAT) \
		--lon-col $(LON) \
		--uncertainty $(UNCERTAINTY) \
		--resolution $(RESOLUTION)

# -----------------------------------------------------------------------------
# Diagnóstico
# -----------------------------------------------------------------------------
check-tifs:
	$(COMPOSE) exec worker bash -c \
		'find /data/rasters -name "*.tif" | sort | xargs ls -lh 2>/dev/null || echo "Nenhum .tif encontrado"'

check-iso:
	$(COMPOSE) exec worker bash -c \
		'find /data/isoscapes -name "*.tif" -o -name "metrics.json" | sort | xargs ls -lh 2>/dev/null || echo "Nenhum isoscape encontrado"'

check-log-r:
	$(COMPOSE) exec worker bash -c \
		'cat /tmp/debug_rasters_r/log.txt 2>/dev/null || echo "Log não encontrado em /tmp/debug_rasters_r/log.txt"'

check-log-py:
	$(COMPOSE) exec worker bash -c \
		'cat /tmp/debug_rasters_py/log.txt 2>/dev/null || echo "Log não encontrado em /tmp/debug_rasters_py/log.txt"'
