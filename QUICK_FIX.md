# 🚀 Solução Rápida: gen_rasters_task não executa

## Sua Situação
- ✓ Job é criado com sucesso
- ✗ `gen_rasters_task.delay(job.id)` retorna um ID de task
- ✗ Nada acontece (nenhum raster gerado)

## Causa Mais Provável
**O Worker Celery NÃO está rodando** ou **Redis não está acessível**.

## Solução em 3 Passos

### 1️⃣ Inicie o Docker Compose
```bash
cd /home/jnov/isoscape-platform
docker-compose down
docker-compose up -d
```

**Verifique se tudo está rodando:**
```bash
docker-compose ps
```

Todos devem estar com `Up` como status.

### 2️⃣ Verifique os Logs do Worker
```bash
docker-compose logs -f worker
```

Você deve ver mensagens como:
```
worker_1  | [tasks]
worker_1  |   . apps.jobs.tasks.gen_rasters_task
worker_1  |   . apps.jobs.tasks.run_isoscape_task
worker_1  |
worker_1  | [2024-04-16 12:00:00,000: INFO/MainProcess] Connected to redis://redis:6379/0
```

Se NÃO ver essas mensagens → **o worker não iniciou corretamente**.

### 3️⃣ Teste a Tarefa
No Django shell:
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

# Criar dados de teste
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

# Disparar tarefa
result = gen_rasters_task.delay(job.id)
print(f"Task ID: {result.id}")
```

**Agora observe o worker:**
```bash
docker-compose logs -f worker
```

Você deve ver a tarefa sendo processada.

## Alternativa: Teste Sem Celery (Debug)

Se o worker ainda não funcionar, teste a execução direta:

```bash
cd /home/jnov/isoscape-platform/backend
python test_gen_rasters_local.py
```

Isso vai:
1. Criar um job
2. Executar `gen_rasters_task` **sincrono** (sem Celery)
3. Mostrar EXATAMENTE qual é o erro

Se isso funcionar → **o problema é Celery/Redis, não o script R**
Se isso falhar → **o problema é o script R ou dados faltando**

## Verificação Rápida

```bash
# Redis rodando?
docker-compose exec redis redis-cli PING
# Deve retornar: PONG

# Shapefile existe?
docker-compose exec backend ls -la /data/shapefiles/amazonia_legal.shp

# Rscript no worker?
docker-compose exec worker which Rscript

# Python no worker?
docker-compose exec worker python --version
```

## Próximas Ações

1. **Execute:** `docker-compose logs -f worker`
2. **Copie a saída** (qualquer erro que ver)
3. **Teste:** `python test_gen_rasters_local.py`
4. **Se ainda falhar** → cole o erro aqui

---

**Dica:** Se tudo está OK mas a tarefa não executa, reinicie o worker:
```bash
docker-compose restart worker
```
