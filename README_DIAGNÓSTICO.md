# 🎯 Diagnóstico: gen_rasters_task não executa

## Status
- ✓ Job criado com sucesso (ID = 1)
- ✓ Task enfileirada (`gen_rasters_task.delay(job.id)` retorna Task ID)
- ✗ **Nada acontece:** nenhum raster gerado, nenhum arquivo baixado

## Causa Provável
**Worker Celery não está rodando** ou **Redis não está acessível**

## Solução em 2 Comandos

```bash
# 1. Iniciar Docker
cd /home/jnov/isoscape-platform
docker-compose up -d

# 2. Verificar status
docker-compose ps
```

Se tudo estiver `Up`, o problema foi resolvido!

## Próximo Teste

```bash
# Terminal 1: Ver logs do worker
docker-compose logs -f worker

# Terminal 2: Django shell
cd backend
python manage.py shell
```

```python
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
    config={
        "study_area_id": sa.id,
        "study_area_name": sa.name,
        "shapefile_path": "/data/shapefiles/amazonia_legal.shp",
        "variables": ["tavg"],
        "resolution": "10",
        "skip_existing": True,
    }
)

result = gen_rasters_task.delay(job.id)
print(f"Job ID: {job.id}, Task ID: {result.id}")
```

Agora veja o Terminal 1. Você deve ver a tarefa sendo processada!

## Se Ainda Não Funcionar

📖 **Leia os guias:**
1. `SOLUCAO.md` - Resumo completo
2. `QUICK_FIX.md` - Solução rápida em 3 passos  
3. `CHECKLIST.md` - Lista de verificação detalhada
4. `TROUBLESHOOTING_CELERY.md` - Guia de troubleshooting completo
5. `DEBUG_GUIDE.txt` - Diagramas visuais

🧪 **Scripts de teste:**
```bash
cd backend

# Teste automático da infraestrutura
./quick_test.sh

# Teste sem Celery (execução síncrona)
python test_gen_rasters_local.py
```

## Estrutura Entregue

```
/home/jnov/isoscape-platform/
├── SOLUCAO.md                      ← Leia primeiro!
├── QUICK_FIX.md                    ← Solução rápida
├── CHECKLIST.md                    ← Verificação passo a passo
├── TROUBLESHOOTING_CELERY.md       ← Guia completo
├── DEBUG_GUIDE.txt                 ← Diagramas
└── backend/
    ├── quick_test.sh               ← Teste automático
    ├── test_gen_rasters_local.py   ← Teste sem Celery
    └── diagnose_celery.sh          ← Diagnóstico Celery
```

## Resumo do Problema

| Componente | Status | Verificação |
|-----------|--------|-------------|
| Docker | ? | `docker-compose ps` |
| Redis | ? | `docker-compose exec redis redis-cli PING` |
| Worker | ? | `docker-compose logs worker \| grep "Connected"` |
| Rscript | ? | `docker-compose exec worker which Rscript` |
| Shapefile | ? | `docker-compose exec worker ls /data/shapefiles/amazonia_legal.shp` |

**Execute cada comando acima e marque se passou ✓ ou falhou ✗**

## Fluxo Esperado

```
Django Shell
    ↓
gen_rasters_task.delay(job.id)  ← Você aqui
    ↓
Redis (fila)
    ↓
Worker Celery (recebe)
    ↓
Executa: gen_rasters_task(job.id)
    ↓
Chama: Rscript gen_rasters.R ...
    ↓
Script R:
    ├─ Carrega shapefile
    ├─ Baixa WorldClim
    ├─ Recorta por área
    └─ Salva rasters
    ↓
Atualiza: job.status = COMPLETED
    ↓
Rasters em: /data/rasters/{project_id}/amazonia_legal/*.tif
```

**Se tudo funcionar:** você verá arquivos `.tif` em `/data/rasters/`

---

**Próxima ação:** Execute `docker-compose up -d` e `docker-compose ps` para verificar!
