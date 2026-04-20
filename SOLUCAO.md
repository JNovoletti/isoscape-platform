📌 RESUMO DA SOLUÇÃO - gen_rasters_task não executa
═══════════════════════════════════════════════════════════════════════════════

## LEIA PRIMEIRO

Você criou um Job corretamente:
  ✓ job = Job.objects.create(...)  → ID 1 criado
  ✓ result = gen_rasters_task.delay(job.id)  → Task enfileirada

Mas a tarefa **não está sendo executada** porque:
  ❌ O Worker Celery provavelmente está parado ou desconectado do Redis

## SOLUÇÃO RÁPIDA (5 minutos)

### Passo 1: Inicie o Docker
```bash
cd /home/jnov/isoscape-platform
docker-compose up -d
```

### Passo 2: Verifique se tudo está rodando
```bash
docker-compose ps
```

Deve mostrar todos os containers `Up`.

### Passo 3: Teste o Worker
```bash
cd backend
./quick_test.sh
```

Este script verifica:
  ✓ Todos os containers estão rodando
  ✓ Redis está acessível
  ✓ Rscript está instalado
  ✓ Shapefile existe
  ✓ Worker está conectado ao Redis

### Passo 4: Monitore os logs enquanto testa
Terminal 1:
```bash
docker-compose logs -f worker
```

Terminal 2:
```bash
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

# DISPARA A TAREFA
result = gen_rasters_task.delay(job.id)
print(f"Task ID: {result.id}")
print(f"Job ID: {job.id}")
```

**Agora veja o Terminal 1 (logs do worker)**
Você deve ver:
```
Received task: apps.jobs.tasks.gen_rasters_task[...]
Started gen_rasters...
[→] Carregando shapefile...
[✓] Shapefile carregado: amazonia_legal.shp
...
```

## SE AINDA NÃO FUNCIONAR

### Opção A: Teste sem Celery (execução síncrona)
```bash
cd backend
python test_gen_rasters_local.py
```

Se isso funcionar → **problema é Celery/Redis**
Se isso falhar → **problema é o script R**

### Opção B: Verifique dados de erro
```python
job.refresh_from_db()
print(f"Status: {job.status}")
print(f"Error: {job.error_message}")
print(f"Log:\n{job.log}")
```

### Opção C: Reinicie tudo
```bash
docker-compose down
docker-compose up -d
```

## ARQUIVOS DE AJUDA

| Arquivo | Descrição |
|---------|-----------|
| `QUICK_FIX.md` | Solução rápida em 3 passos |
| `TROUBLESHOOTING_CELERY.md` | Guia completo de troubleshooting |
| `DEBUG_GUIDE.txt` | Diagrama visual do fluxo |
| `backend/quick_test.sh` | Script de diagnóstico automático |
| `backend/test_gen_rasters_local.py` | Teste da tarefa sem Celery |

## COMANDOS ÚTEIS

```bash
# Ver status do Docker
docker-compose ps

# Ver logs do worker em tempo real
docker-compose logs -f worker

# Testar Redis
docker-compose exec redis redis-cli PING

# Entrar no shell do worker
docker-compose exec worker bash

# Limpar fila Redis (⚠️ cuidado!)
docker-compose exec redis redis-cli FLUSHDB

# Reiniciar apenas o worker
docker-compose restart worker

# Rebuild do worker (se mudou Dockerfile)
docker-compose up -d --build worker
```

## PRÓXIMAS AÇÕES

1. Execute: `docker-compose ps`
2. Execute: `cd backend && ./quick_test.sh`
3. Se tudo OK: siga Passo 4 acima
4. Se algo falhar: leia TROUBLESHOOTING_CELERY.md ou DEBUG_GUIDE.txt
5. Se ainda não funcionar: execute `test_gen_rasters_local.py` para diagnóstico

═══════════════════════════════════════════════════════════════════════════════

**TL;DR:**
Seu código está correto. O problema é a infraestrutura (Docker/Redis).
Inicie o Docker e tudo deve funcionar.

docker-compose up -d
