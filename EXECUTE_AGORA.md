🎯 AÇÃO IMEDIATA - Execute AGORA
═══════════════════════════════════════════════════════════════════════════════

## PASSO 1: Inicie o Docker (1 minuto)

Abra um terminal e execute:

```bash
cd /home/jnov/isoscape-platform
docker-compose up -d
```

Aguarde 30 segundos para tudo iniciar.

Verifique:
```bash
docker-compose ps
```

**Esperado:**
```
NAME                  STATUS
isoscape_db           Up
isoscape_redis        Up
isoscape_backend      Up
isoscape_worker       Up
```

Se TODOS estão "Up" → ✓ Passe para PASSO 2

Se algum está "Down" → problema de Docker, releia QUICK_FIX.md

───────────────────────────────────────────────────────────────────────────────

## PASSO 2: Teste a Tarefa (5 minutos)

Em um terminal, veja os logs do worker:

```bash
docker-compose logs -f worker
```

Mantém este terminal aberto!

Em OUTRO terminal, faça:

```bash
cd /home/jnov/isoscape-platform/backend
python manage.py shell
```

Cole este código:

```python
from django.contrib.auth import get_user_model
from apps.projects.models import Project
from apps.shapefiles.models import StudyArea
from apps.jobs.models import Job
from apps.jobs.tasks import gen_rasters_task

User = get_user_model()

# Criar usuário
user = User.objects.create_user(
    username="teste",
    email="teste@example.com",
    password="123456"
)

# Criar projeto
project = Project.objects.create(
    owner=user,
    name="Teste Gen Rasters",
    isotope_type="d13C"
)

# Criar study area
sa = StudyArea.objects.create(
    name="amazonia_legal",
    file_path="/data/shapefiles/amazonia_legal.shp",
    is_preset=True,
)

# Criar job
job = Job.objects.create(
    project=project,
    created_by=user,
    job_type=Job.JobType.GEN_RASTERS,
    config={
        "study_area_id": sa.id,
        "study_area_name": sa.name,
        "shapefile_path": "/data/shapefiles/amazonia_legal.shp",
        "variables": ["tavg"],  # Apenas 1 variável para teste rápido
        "resolution": "10",
        "skip_existing": True,
    }
)

# DISPARA A TAREFA
print(f"Job criado: ID={job.id}")
result = gen_rasters_task.delay(job.id)
print(f"Task disparada: ID={result.id}")
```

───────────────────────────────────────────────────────────────────────────────

## PASSO 3: Observe os Logs (2-10 minutos)

Volte ao PRIMEIRO terminal (com logs do worker) e observe.

**ESPERADO ✓:**
```
[tasks]
  . apps.jobs.tasks.gen_rasters_task
  
Received task: apps.jobs.tasks.gen_rasters_task[...]
[2024-04-16 12:00:00] [→] Job gen_rasters iniciado: 1
[2024-04-16 12:00:01] [→] Carregando shapefile...
[2024-04-16 12:00:02] [✓] Shapefile carregado: amazonia_legal.shp
[2024-04-16 12:00:03] [→] Baixando: tavg
[2024-04-16 12:00:45] [✓] Salvo: amazonia_legal_10arc_tavg_mean.tif
[2024-04-16 12:00:46] [✓] metrics.json salvo
[2024-04-16 12:00:46] [★] gen_rasters concluído
Task received: apps.jobs.tasks.gen_rasters_task[...] succeeded
```

**NÃO ESPERADO ✗:**
- Nenhuma mensagem aparece → Worker não está conectado ao Redis
- Erro de permissão → Problema com diretórios
- "Rscript not found" → R não instalado

───────────────────────────────────────────────────────────────────────────────

## PASSO 4: Verifique o Resultado (1 minuto)

De volta ao Django shell (SEGUNDO terminal):

```python
job.refresh_from_db()
print(f"Status: {job.status}")
print(f"Log (últimas 500 chars):\n{job.log[-500:]}")

# Se falhou:
if job.status == Job.Status.FAILED:
    print(f"Erro: {job.error_message}")
```

**ESPERADO ✓:**
```
Status: COMPLETED
Log: [...gen_rasters concluído...]
```

**NÃO ESPERADO ✗:**
```
Status: RUNNING   # Nunca acabou
Status: FAILED    # Erro ocorreu
```

───────────────────────────────────────────────────────────────────────────────

## PASSO 5: Verifique se Rasters Foram Gerados (30 seg)

No PRIMEIRO terminal (ou novo terminal):

```bash
docker-compose exec worker ls /data/rasters/*/amazonia_legal/*.tif
```

**ESPERADO ✓:**
```
/data/rasters/1/amazonia_legal/amazonia_legal_10arc_tavg_mean.tif
```

**NÃO ESPERADO ✗:**
```
ls: cannot access '/data/rasters/*/amazonia_legal/*.tif': No such file or directory
```

═══════════════════════════════════════════════════════════════════════════════

## ✅ SUCESSO!

Se chegou aqui e:
  ✓ Viu "Received task" nos logs
  ✓ job.status = COMPLETED
  ✓ Rasters em /data/rasters/
  
Então **TUDO ESTÁ FUNCIONANDO!** 🎉

═══════════════════════════════════════════════════════════════════════════════

## ❌ FALHOU?

Se algo deu errado:

1. Releia os logs do worker procurando por ERRO
2. Execute: `./backend/quick_test.sh`
3. Leia: QUICK_FIX.md ou TROUBLESHOOTING_CELERY.md
4. Se muito confuso: execute `python ./backend/test_gen_rasters_local.py`

═══════════════════════════════════════════════════════════════════════════════

**Tempo total esperado: 10-15 minutos**

Comece pelo PASSO 1! ⬆️
