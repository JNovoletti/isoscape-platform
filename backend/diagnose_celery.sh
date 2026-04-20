#!/bin/bash
# ============================================================================
# Script de diagnóstico para o problema do Celery
# Executa testes para identificar por que gen_rasters_task não está rodando
# ============================================================================

set -e

echo "=========================================="
echo "DIAGNÓSTICO: Celery + gen_rasters_task"
echo "=========================================="
echo

# TEST 1: Redis rodando?
echo "[TEST 1] Redis está acessível?"
if redis-cli -h redis ping > /dev/null 2>&1; then
    echo "✓ Redis respondendo"
else
    if redis-cli -h localhost ping > /dev/null 2>&1; then
        echo "✓ Redis (localhost) respondendo"
    else
        echo "✗ ERRO: Redis NÃO está acessível!"
        echo "  Solução: docker-compose up -d redis"
        exit 1
    fi
fi
echo

# TEST 2: Worker Celery rodando?
echo "[TEST 2] Worker Celery está rodando?"
if pgrep -f "celery.*worker" > /dev/null; then
    echo "✓ Worker Celery está ativo"
else
    echo "✗ AVISO: Worker Celery NÃO está rodando localmente"
    echo "  (Isso é normal se você está em um container separado)"
    echo "  Verifique: docker-compose logs worker"
fi
echo

# TEST 3: Rscript instalado?
echo "[TEST 3] Rscript está instalado?"
if which Rscript > /dev/null 2>&1; then
    echo "✓ Rscript encontrado: $(which Rscript)"
else
    echo "✗ ERRO: Rscript NÃO instalado!"
    echo "  Solução (para o container worker):"
    echo "    apt-get update && apt-get install -y r-base"
    exit 1
fi
echo

# TEST 4: Diretórios de dados existem?
echo "[TEST 4] Diretórios de dados existem?"
for dir in /data/rasters /data/shapefiles /data/worldclim_cache; do
    if [ -d "$dir" ]; then
        echo "✓ $dir existe"
    else
        echo "✗ $dir NÃO existe (será criado)"
        mkdir -p "$dir"
    fi
done
echo

# TEST 5: Shapefile amazonia_legal existe?
echo "[TEST 5] Shapefile amazonia_legal.shp existe?"
if [ -f "/data/shapefiles/amazonia_legal.shp" ]; then
    echo "✓ /data/shapefiles/amazonia_legal.shp encontrado"
else
    echo "✗ ERRO: Shapefile NÃO encontrado!"
    echo "  Verifique: ls -la /data/shapefiles/"
    exit 1
fi
echo

echo "=========================================="
echo "SE TODOS OS TESTES PASSARAM:"
echo "=========================================="
echo
echo "Próximos passos:"
echo "1. Execute no shell Django:"
echo "   python manage.py shell"
echo
echo "2. Cole este código:"
cat << 'DJANGO_CODE'
from django.contrib.auth import get_user_model
from apps.projects.models import Project
from apps.shapefiles.models import StudyArea
from apps.jobs.models import Job
from apps.jobs.tasks import gen_rasters_task

User = get_user_model()
user = User.objects.create_user(username="teste", email="teste@example.com", password="123456")
project = Project.objects.create(owner=user, name="Teste", isotope_type="d13C")
sa = StudyArea.objects.create(name="amazonia_legal", file_path="/data/shapefiles/amazonia_legal.shp", is_preset=True)
job = Job.objects.create(
    project=project,
    created_by=user,
    job_type=Job.JobType.GEN_RASTERS,
    config={"study_area_id": sa.id, "study_area_name": sa.name, "shapefile_path": sa.file_path, "variables": ["tavg"], "resolution": "10", "skip_existing": True}
)
print(f"Job criado: ID={job.id}")
result = gen_rasters_task.delay(job.id)
print(f"Task ID: {result.id}")
print(f"Status: {result.status}")
DJANGO_CODE
echo
echo "3. Monitore o worker:"
echo "   docker-compose logs -f worker"
echo
echo "4. Verifique se rasters foram gerados:"
echo "   ls -la /data/rasters/*/amazonia_legal/"
