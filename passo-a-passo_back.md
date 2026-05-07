# Teste do backend — gen_rasters + run_isoscape (R e Python)

---

## Pré-requisitos de dados

Antes de iniciar, certifique-se de que os seguintes arquivos existem dentro do container:

| Arquivo | Caminho esperado |
|---|---|
| Shapefile da área de estudo | `/data/shapefiles/amazonia_legal.shp` (+ .dbf, .shx, .prj) |
| Dataset de amostras | `/data/datasets/madeiras.csv` |
| Cache WorldClim (criado automaticamente) | `/data/worldclim_cache/` |

Se o shapefile ainda não estiver no container:
```bash
docker compose cp caminho/local/amazonia_legal.shp worker:/data/shapefiles/
# repita para .dbf .shx .prj
```

---

## 1. Subir serviços

```bash
make up back
```

---

## 2. Migrar banco

```bash
make migrations
make migrate
```

---

## 3. Criar dados mínimos no shell Django

```bash
make shell
```

Cole no shell:

```python
from django.contrib.auth import get_user_model
from apps.projects.models import Project
from apps.shapefiles.models import StudyArea
from apps.jobs.models import Job

User = get_user_model()

# Criar superusuário (pular se já existir)
user, created = User.objects.get_or_create(username="admin")
if created:
    user.set_password("admin123")
    user.is_superuser = True
    user.is_staff = True
    user.save()

project = Project.objects.create(owner=user, name="Teste", isotope_type="d13C")
print("Project:", project.id)

sa = StudyArea.objects.create(
    name="amazonia_legal",
    file_path="/data/shapefiles/amazonia_legal.shp",
    is_preset=True,
)
print("StudyArea:", sa.id)
```

Anote os valores impressos (`project.id` e `sa.id`) — você vai precisar deles abaixo.

---

## 4. Etapa A — Geração de rasters (gen_rasters)

### 4a. Engine R

```python
from apps.jobs.models import Job
from apps.jobs.tasks import gen_rasters_task

# Substitua PROJECT_ID e SA_ID pelos valores anotados acima
PROJECT_ID = 1
SA_ID      = 1

job_r = Job.objects.create(
    project_id=PROJECT_ID,
    created_by=user,
    job_type=Job.JobType.GEN_RASTERS,
    status=Job.Status.PENDING,
    config={
        "execution_engine": "r",
        "study_area_id":    SA_ID,
        "study_area_name":  "amazonia_legal",
        "shapefile_path":   "/data/shapefiles/amazonia_legal.shp",
        "variables":        ["tavg"],   # comece com 1 variável para testar rápido
        "resolution":       "10",       # 10 arc-min = menor resolução = mais rápido
        "skip_existing":    True,
    }
)
print("Job R gen_rasters:", job_r.id)
gen_rasters_task.delay(job_r.id)
```

### 4b. Engine Python

```python
job_py = Job.objects.create(
    project_id=PROJECT_ID,
    created_by=user,
    job_type=Job.JobType.GEN_RASTERS,
    status=Job.Status.PENDING,
    config={
        "execution_engine": "python",
        "study_area_id":    SA_ID,
        "study_area_name":  "amazonia_legal",
        "shapefile_path":   "/data/shapefiles/amazonia_legal.shp",
        "variables":        ["tavg"],
        "resolution":       "10",
        "skip_existing":    True,
    }
)
print("Job Python gen_rasters:", job_py.id)
gen_rasters_task.delay(job_py.id)
```

### Verificar status de gen_rasters
#### R
```python
# Troque JOB_ID pelo id retornado acima
import time
for _ in range(12):       # checa por até 2 minutos
    job_r.refresh_from_db()
    print(f"[R]  status={job_r.status} | step={job_r.progress_step}")
    if job_r.status in ("completed", "failed"):
        break
    time.sleep(10)

if job_r.status == "failed":
    print("ERRO R:", job_r.error_message)
    print("LOG R:\n", job_r.log[:3000])
```

#### Python
```python
# Troque JOB_ID pelo id retornado acima
import time
for _ in range(12):       # checa por até 2 minutos
    job_py.refresh_from_db()
    print(f"[PY]  status={job_py.status} | step={job_py.progress_step}")
    if job_py.status in ("completed", "failed"):
        break
    time.sleep(10)

if job_py.status == "failed":
    print("ERRO PY:", job_py.error_message)
    print("LOG PY:\n", job_py.log[:3000])
```


---

## 5. Etapa B — Geração de isoscape (run_isoscape)

> ⚠️ Execute a Etapa A primeiro e confirme status=completed antes de prosseguir.
> O `raster_dir` deve apontar para onde os .tif foram gerados.

### 5a. Engine R

```python
from apps.jobs.tasks import run_isoscape_task

job_iso_r = Job.objects.create(
    project_id=PROJECT_ID,
    created_by=user,
    job_type=Job.JobType.RUN_ISOSCAPE,
    status=Job.Status.PENDING,
    config={
        "execution_engine": "r",
        "dataset_path":     "/data/datasets/madeiras.csv",
        "raster_dir":       f"/data/rasters/{PROJECT_ID}/amazonia_legal",
        "response_col":     "d13C_wood",
        "lat_col":          "latitude",
        "lon_col":          "longitude",
        "uncertainty":      "quantile_rf",
        "resolution":       "10",
    }
)
print("Job R run_isoscape:", job_iso_r.id)
run_isoscape_task.delay(job_iso_r.id)
```

### 5b. Engine Python

```python
job_iso_py = Job.objects.create(
    project_id=PROJECT_ID,
    created_by=user,
    job_type=Job.JobType.RUN_ISOSCAPE,
    status=Job.Status.PENDING,
    config={
        "execution_engine": "python",
        "dataset_path":     "/data/datasets/madeiras.csv",
        "raster_dir":       f"/data/rasters/{PROJECT_ID}/amazonia_legal",
        "response_col":     "d13C_wood",
        "lat_col":          "latitude",
        "lon_col":          "longitude",
        "uncertainty":      "quantile_rf",   # ou "bootstrap"
        "resolution":       "10",
    }
)
print("Job Python run_isoscape:", job_iso_py.id)
run_isoscape_task.delay(job_iso_py.id)
```

### Verificar status de run_isoscape

```python
for _ in range(36):       # checa por até 6 minutos (VSURF pode demorar)
    job_iso_r.refresh_from_db()
    print(f"[R]  status={job_iso_r.status} | step={job_iso_r.progress_step}")
    if job_iso_r.status in ("completed", "failed"):
        break
    time.sleep(10)

if job_iso_r.status == "failed":
    print("ERRO R:", job_iso_r.error_message)
    print("LOG R:\n", job_iso_r.log[:3000])
```

---

## 6. Ver logs do worker em tempo real

```bash
docker compose logs -f worker
```

---

## 7. Debug: problema "Carregando rasters..."

Este erro indica que o job de isoscape não encontrou .tif no `raster_dir`.

```bash
# 1. Verificar se os .tif foram gerados (substitua PROJECT_ID)
docker compose exec worker bash -c 'ls -lh /data/rasters/1/amazonia_legal/*.tif 2>/dev/null || echo "NENHUM TIF ENCONTRADO"'

# 2. Verificar o log do gen_rasters (substitua JOB_ID)
docker compose exec worker bash -c 'cat /data/rasters/1/amazonia_legal/log.txt'

# 3. Verificar permissões do worldclim cache
docker compose exec worker bash -c 'ls -la /data/worldclim_cache/'

# 4. Rodar gen_rasters.R manualmente para ver output completo
docker compose exec worker Rscript r_scripts/gen_rasters.R \
    --job-id debug-manual \
    --shapefile /data/shapefiles/amazonia_legal.shp \
    --output-dir /tmp/debug_rasters \
    --worldclim-dir /data/worldclim_cache \
    --variables tavg \
    --resolution 10 \
    --skip-existing FALSE

# 5. Rodar run_isoscape.R manualmente (só depois de ter .tif)
docker compose exec worker Rscript r_scripts/run_isoscape.R \
    --job-id debug-manual \
    --dataset-path /data/datasets/madeiras.csv \
    --raster-dir /data/rasters/1/amazonia_legal \
    --output-dir /tmp/debug_iso \
    --response-col d13C_wood \
    --lat-col latitude \
    --lon-col longitude \
    --uncertainty quantile_rf \
    --resolution 10

# 6. Rodar gen_rasters.py manualmente
docker compose exec worker python python_scripts/gen_rasters.py \
    --job-id debug-py \
    --shapefile /data/shapefiles/amazonia_legal.shp \
    --output-dir /tmp/debug_rasters_py \
    --worldclim-dir /data/worldclim_cache \
    --variables tavg \
    --resolution 10 \
    --skip-existing false

# 7. Rodar run_isoscape.py manualmente
docker compose exec worker python python_scripts/run_isoscape.py \
    --job-id debug-py \
    --dataset-path /data/datasets/madeiras.csv \
    --raster-dir /data/rasters/1/amazonia_legal \
    --output-dir /tmp/debug_iso_py \
    --response-col d13C_wood \
    --lat-col latitude \
    --lon-col longitude \
    --uncertainty quantile_rf \
    --resolution 10
```

---

## 8. Verificar outputs finais

```bash
# Isoscapes gerados
docker compose exec worker bash -c 'ls -lh /data/isoscapes/*/'

# metrics.json de um job (substitua JOB_ID)
docker compose exec worker bash -c 'cat /data/isoscapes/1/metrics.json'
```

---

## Notas

| Item | Detalhe |
|---|---|
| Engine por job | Definida via `config.execution_engine`: `"r"` ou `"python"` |
| Fallback automático | Se `SCRIPT_ENGINE_FALLBACK=True` no settings e o Python falhar, tenta R |
| Engine usada no log | O log do job começa com `[engine] r`, `[engine] python` ou `[engine] r (fallback)` |
| gen_rasters R trava | Quase sempre é timeout de rede no download do WorldClim ou falta de permissão em `worldclim_dir` — veja o log em `/data/rasters/<project_id>/<study_area_name>/log.txt` |
| Tempo esperado | gen_rasters: 2–10 min por variável (depende da banda larga). run_isoscape: 2–5 min (VSURF/RFE é o gargalo) |
| Python scripts | Funcionalmente equivalentes ao R. `gen_rasters.py` baixa via urllib (sem dependências extras). `run_isoscape.py` usa sklearn RF + GBR quantile |
