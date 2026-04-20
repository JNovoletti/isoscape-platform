#!/bin/bash
# =============================================================================
# TESTE RÁPIDO: Verificar se Celery/Redis estão funcionando
# =============================================================================

set -e

RESET='\033[0m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'

echo -e "${BLUE}════════════════════════════════════════════════════════════${RESET}"
echo -e "${BLUE}  TESTE RÁPIDO: Celery + gen_rasters_task${RESET}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${RESET}\n"

# TEST 1: Docker Compose
echo -e "${BLUE}[1] Verificando containers Docker...${RESET}"
cd /home/jnov/isoscape-platform

if ! command -v docker-compose &> /dev/null; then
    echo -e "${RED}✗ docker-compose não instalado!${RESET}"
    exit 1
fi

SERVICES=("db" "redis" "backend" "worker")
for service in "${SERVICES[@]}"; do
    status=$(docker-compose ps -q $service 2>/dev/null | wc -l)
    if [ $status -gt 0 ]; then
        is_running=$(docker-compose exec -T $service true 2>/dev/null && echo "yes" || echo "no")
        if [ "$is_running" = "yes" ]; then
            echo -e "  ${GREEN}✓${RESET} $service está rodando"
        else
            echo -e "  ${RED}✗${RESET} $service parado"
        fi
    else
        echo -e "  ${RED}✗${RESET} $service não existe"
    fi
done

echo -e "\n${BLUE}[2] Testando Redis...${RESET}"
if docker-compose exec -T redis redis-cli PING &>/dev/null; then
    echo -e "  ${GREEN}✓${RESET} Redis respondendo: PONG"
else
    echo -e "  ${RED}✗${RESET} Redis não respondendo"
    echo -e "  ${YELLOW}→${RESET} Execute: docker-compose restart redis"
    exit 1
fi

echo -e "\n${BLUE}[3] Testando Rscript no worker...${RESET}"
if docker-compose exec -T worker which Rscript &>/dev/null; then
    rscript_path=$(docker-compose exec -T worker which Rscript)
    echo -e "  ${GREEN}✓${RESET} Rscript encontrado: $rscript_path"
else
    echo -e "  ${RED}✗${RESET} Rscript não encontrado"
    echo -e "  ${YELLOW}→${RESET} R não está instalado no container"
    exit 1
fi

echo -e "\n${BLUE}[4] Verificando shapefile...${RESET}"
if docker-compose exec -T worker test -f /data/shapefiles/amazonia_legal.shp; then
    echo -e "  ${GREEN}✓${RESET} /data/shapefiles/amazonia_legal.shp existe"
else
    echo -e "  ${RED}✗${RESET} Shapefile não encontrado"
    exit 1
fi

echo -e "\n${BLUE}[5] Verificando logs do worker...${RESET}"
logs=$(docker-compose logs worker 2>&1 | tail -20)
if echo "$logs" | grep -q "gen_rasters_task"; then
    echo -e "  ${GREEN}✓${RESET} Task descoberta pelo worker"
else
    echo -e "  ${YELLOW}!${RESET} Task não aparece nos logs iniciais"
fi

if echo "$logs" | grep -q "Connected to redis"; then
    echo -e "  ${GREEN}✓${RESET} Worker conectado ao Redis"
else
    echo -e "  ${RED}✗${RESET} Worker NÃO conectado ao Redis"
fi

echo -e "\n${BLUE}════════════════════════════════════════════════════════════${RESET}"
echo -e "${GREEN}✓ Tudo parece OK!${RESET}\n"

echo -e "Próximo passo:"
echo -e "  1. Abra Django shell:"
echo -e "     ${YELLOW}python manage.py shell${RESET}"
echo -e ""
echo -e "  2. Cole este código:"
cat << 'PYTHON'
from django.contrib.auth import get_user_model
from apps.projects.models import Project
from apps.shapefiles.models import StudyArea
from apps.jobs.models import Job
from apps.jobs.tasks import gen_rasters_task

User = get_user_model()
user = User.objects.create_user(username="teste", email="teste@example.com", password="123456")
project = Project.objects.create(owner=user, name="Teste", isotope_type="d13C")
sa = StudyArea.objects.create(name="amazonia_legal", file_path="/data/shapefiles/amazonia_legal.shp", is_preset=True)
job = Job.objects.create(project=project, created_by=user, job_type=Job.JobType.GEN_RASTERS, config={"study_area_id": sa.id, "study_area_name": sa.name, "shapefile_path": "/data/shapefiles/amazonia_legal.shp", "variables": ["tavg"], "resolution": "10", "skip_existing": True})
result = gen_rasters_task.delay(job.id)
print(f"Task ID: {result.id}, Job ID: {job.id}")
PYTHON

echo -e "\n  3. Em outro terminal, veja os logs:"
echo -e "     ${YELLOW}docker-compose logs -f worker${RESET}"
echo -e ""
echo -e "  4. Após terminar, verifique os rasters:"
echo -e "     ${YELLOW}docker-compose exec worker ls /data/rasters/*/amazonia_legal/${RESET}"
