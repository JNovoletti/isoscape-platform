# Teste do backend — prepare_dataset + gen_rasters + run_isoscape + run_assign (R e Python)

> **Mudanças desta versão:**
> - Nova **Etapa 0 — `prepare_dataset`**: agrega múltiplas amostras na mesma `(latitude, longitude)` em uma única observação. Necessária para datasets como `madeiras.csv` que têm várias medições por árvore (`ratio_point`).
> - Outputs ganharam sufixo de engine: `_r.tif` (R) e `_py.tif` (Python) — você consegue rodar os dois e comparar lado a lado.
> - Cache do WorldClim é compartilhado entre R e Python — quem rodar primeiro baixa, o outro reaproveita.
> - Nova task **`run_assign`** (atribuição bayesiana de origem, equivalente ao `04_assign.R` do curso).
> - `run_isoscape` agora filtra rasters pelo sufixo: por padrão R lê `*_r.tif` e Python lê `*_py.tif`. Para cruzar engines, passe `raster_suffix` no config.

---

## Pré-requisitos de dados

Antes de iniciar, certifique-se de que os seguintes arquivos existem dentro do container:

| Arquivo | Caminho esperado |
|---|---|
| Shapefile da área de estudo | `/data/shapefiles/amazonia_legal.shp` (+ .dbf, .shx, .prj) |
| Shapefile das regiões (UFs) — só para `run_assign` | `/data/shapefiles/fu.shp` (+ .dbf, .shx, .prj) |
| Dataset de amostras | `/data/datasets/madeiras.csv` |
| Dataset de unknowns — só para `run_assign` | `/data/datasets/unknowns.csv` |
| Cache WorldClim (criado automaticamente, compartilhado R↔Python) | `/data/worldclim_cache/` |

Se algum shapefile ainda não estiver no container:
```bash
docker compose cp caminho/local/amazonia_legal.shp worker:/data/shapefiles/
# repita para .dbf .shx .prj (e fu.shp se for usar run_assign)
```

### Dependência extra para paridade total Python ↔ R no `run_isoscape`

O `run_isoscape.py` usa o pacote **`quantile-forest`** como equivalente direto do `ranger::ranger(..., quantreg=TRUE)` do R. Se não estiver instalado, ele cai para um fallback com GBR — funciona, mas o mapa de incerteza fica menos fiel ao R.

```bash
docker compose exec worker pip install quantile-forest
# (ou adicione ao requirements.txt do worker)
```

### Dataset de unknowns (para `run_assign`)

`unknowns.csv` precisa ter no mínimo: coluna `ID`, coordenadas (`x`,`y` **ou** `longitude`,`latitude`), e a coluna isotópica (ex: `d13C_wood`). Exemplo mínimo:

```csv
ID,x,y,d13C_wood
103,-60.02,-3.10,-29.42
117,-55.85,-12.10,-26.30
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

> Ambas as engines compartilham o mesmo cache em `/data/worldclim_cache/climate/wc2.1_{res}m/`.
> Se rodar a R primeiro, a Python reaproveita os downloads (e vice-versa).
> Cada engine grava saídas **separadas** com sufixo `_r` ou `_py`.

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

Esperado em `/data/rasters/1/amazonia_legal/`:
```
amazonia_legal_10arc_tavg_mean_r.tif
log.txt
metrics.json
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

Esperado em `/data/rasters/1/amazonia_legal/`:
```
amazonia_legal_10arc_tavg_mean_r.tif    ← gerado pela engine R
amazonia_legal_10arc_tavg_mean_py.tif   ← gerado pela engine Python
log.txt
metrics.json
```

### Verificar status de gen_rasters

#### R
```python
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

### Comparar os outputs R vs Python

Depois que os dois rodaram:

```python
import numpy as np
import rasterio
from rasterio.transform import rowcol

# =========================================================
# Comparar outputs R vs Python usando coordenadas geográficas
# (evita problemas de grids desalinhados)
# =========================================================

path_r = "/data/rasters/5/amazonia_legal/amazonia_legal_10arc_tavg_mean_r.tif"
path_py = "/data/rasters/5/amazonia_legal/amazonia_legal_10arc_tavg_mean_py.tif"

# =========================================================
# Abrir rasters
# =========================================================

with rasterio.open(path_r) as src_r, rasterio.open(path_py) as src_py:

    arr_r = src_r.read(1).astype(np.float64)
    arr_py = src_py.read(1).astype(np.float64)

    # -----------------------------------------------------
    # Metadados
    # -----------------------------------------------------

    print("=== R ===")
    print("shape:", src_r.shape)
    print("crs:", src_r.crs)
    print("res:", src_r.res)
    print("bounds:", src_r.bounds)

    print("\n=== PY ===")
    print("shape:", src_py.shape)
    print("crs:", src_py.crs)
    print("res:", src_py.res)
    print("bounds:", src_py.bounds)

    # -----------------------------------------------------
    # Nodata -> NaN
    # -----------------------------------------------------

    if src_r.nodata is not None:
        arr_r[arr_r == src_r.nodata] = np.nan

    if src_py.nodata is not None:
        arr_py[arr_py == src_py.nodata] = np.nan

    # trata floats inválidos comuns
    arr_r[arr_r < -1e20] = np.nan
    arr_py[arr_py < -1e20] = np.nan

    # =====================================================
    # Gerar pontos aleatórios no bounding box comum
    # =====================================================

    left = max(src_r.bounds.left, src_py.bounds.left)
    right = min(src_r.bounds.right, src_py.bounds.right)

    bottom = max(src_r.bounds.bottom, src_py.bounds.bottom)
    top = min(src_r.bounds.top, src_py.bounds.top)

    np.random.seed(42)

    n_points = 30

    xs = np.random.uniform(left, right, n_points)
    ys = np.random.uniform(bottom, top, n_points)

    diffs = []

    print("\n=== COMPARAÇÃO ===")

    for i, (x, y) in enumerate(zip(xs, ys), start=1):

        try:
            # coordenada -> índice raster
            row_r, col_r = rowcol(src_r.transform, x, y)
            row_py, col_py = rowcol(src_py.transform, x, y)

            val_r = arr_r[row_r, col_r]
            val_py = arr_py[row_py, col_py]

            if np.isfinite(val_r) and np.isfinite(val_py):
                diff = abs(val_r - val_py)
                diffs.append(diff)

                print(
                    f"{i:02d} | "
                    f"lon={x:.4f} lat={y:.4f} | "
                    f"R={val_r:.6f} | "
                    f"PY={val_py:.6f} | "
                    f"diff={diff:.6e}"
                )

        except Exception:
            continue

# =========================================================
# Estatísticas finais
# =========================================================

diffs = np.array(diffs)

print("\n=== RESUMO ===")

if len(diffs) > 0:
    print(f"valid points = {len(diffs)}")
    print(f"diff mean = {np.mean(diffs):.6e}")
    print(f"diff max  = {np.max(diffs):.6e}")

    # esperado:
    # diferenças <= 1e-2
    # geralmente ~1e-6 para float32
else:
    print("Nenhum ponto válido encontrado.")
```

---

## 5. Etapa B — Geração de isoscape (run_isoscape)

> ⚠️ Execute a Etapa A primeiro e confirme `status=completed` antes de prosseguir.
>
> **Importante sobre o `raster_dir`:** o `run_isoscape` filtra os `.tif` pelo sufixo da engine
> — R lê só `*_r.tif`, Python lê só `*_py.tif`. Se quiser que uma engine use os rasters da outra,
> passe `raster_suffix` no config (ex: Python lendo rasters do R: `"raster_suffix": "_r"`).
> Para usar TODOS os `.tif` sem filtro, passe `"raster_suffix": ""`.

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
        "uncertainty":      "quantile_rf",   # ou "bootstrap"
        "resolution":       "10",
        "seed":             1350,
    }
)
print("Job R run_isoscape:", job_iso_r.id)
run_isoscape_task.delay(job_iso_r.id)
```

Esperado em `/data/isoscapes/<job_id>/`:
```
isoscape_r.tif                  ← predição
uncertainty_r.tif               ← desvio padrão (mapa de incerteza)
isoscape_combined_r.tif         ← 2 bandas: [predição, sd] — usado pelo run_assign
dataset_with_vars_r.csv         ← amostras + variáveis extraídas
variable_importance_r.csv
log.txt
metrics.json
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
        "seed":             1350,
    }
)
print("Job Python run_isoscape:", job_iso_py.id)
run_isoscape_task.delay(job_iso_py.id)
```

Esperado em `/data/isoscapes/<job_id>/`:
```
isoscape_py.tif
uncertainty_py.tif
isoscape_combined_py.tif
dataset_with_vars_py.csv
variable_importance_py.csv
log.txt
metrics.json
```

### Cruzar engines (ex: Python usando rasters R)

```python
config={
    "execution_engine": "python",
    "raster_suffix":    "_r",        # ← força Python a ler somente *_r.tif
    # ... resto igual
}
```

### Verificar status de run_isoscape

```python
for _ in range(36):       # checa por até 6 minutos (VSURF é o gargalo)
    job_iso_r.refresh_from_db()
    print(f"[R]  status={job_iso_r.status} | step={job_iso_r.progress_step}")
    if job_iso_r.status in ("completed", "failed"):
        break
    time.sleep(10)

if job_iso_r.status == "failed":
    print("ERRO R:", job_iso_r.error_message)
    print("LOG R:\n", job_iso_r.log[:3000])
```

### Comparar isoscapes R vs Python

```python
import rasterio, numpy as np

iso_r  = rasterio.open(f"/data/isoscapes/{job_iso_r.id}/isoscape_r.tif").read(1)
iso_py = rasterio.open(f"/data/isoscapes/{job_iso_py.id}/isoscape_py.tif").read(1)

iso_r  = np.where(iso_r  == -9999, np.nan, iso_r)
iso_py = np.where(iso_py == -9999, np.nan, iso_py)

print(f"R:  mean={np.nanmean(iso_r):.3f} | range=[{np.nanmin(iso_r):.2f}, {np.nanmax(iso_r):.2f}]")
print(f"Py: mean={np.nanmean(iso_py):.3f} | range=[{np.nanmin(iso_py):.2f}, {np.nanmax(iso_py):.2f}]")
# Não vai bater bit-a-bit (RFs estocásticos), mas com seed=1350 deve ficar próximo.
# Pred_vars selecionadas pelo VSURF têm que ser as mesmas ou muito similares.

# Comparar variáveis selecionadas
import json
m_r  = json.load(open(f"/data/isoscapes/{job_iso_r.id}/metrics.json"))
m_py = json.load(open(f"/data/isoscapes/{job_iso_py.id}/metrics.json"))
print("R  pred_vars:", m_r["pred_vars"])
print("Py pred_vars:", m_py["pred_vars"])
print(f"R  MSE={m_r['MSE']:.4f} R²={m_r['R2']:.4f}")
print(f"Py MSE={m_py['MSE']:.4f} R²={m_py['R2']:.4f}")
```

---

## 6. Etapa C — Atribuição de origem (run_assign) — NOVO

Equivalente backend do `04_assign.R`. Atribui amostras desconhecidas a regiões candidatas
usando o isoscape + Teorema de Bayes (pdRaster + qtlRaster + oddsRatio do `assignR`).

> ⚠️ Requer um isoscape **combinado** (2 bandas: predição + sd) — gerado automaticamente
> pelo `run_isoscape` em `isoscape_combined_r.tif` ou `isoscape_combined_py.tif`.

### 6a. Engine R

```python
from apps.jobs.tasks import run_assign_task

job_assign_r = Job.objects.create(
    project_id=PROJECT_ID,
    created_by=user,
    job_type=Job.JobType.RUN_ASSIGN,   # ⚠️ adicionar este valor no enum JobType
    status=Job.Status.PENDING,
    config={
        "execution_engine": "r",
        "isoscape_path":    f"/data/isoscapes/{job_iso_r.id}/isoscape_combined_r.tif",
        "unknown_path":     "/data/datasets/unknowns.csv",
        "regions_shp":      "/data/shapefiles/fu.shp",
        "regions_field":    "ADM1_PT",
        "regions_filter":   ["Amazonas", "Mato Grosso"],
        "response_col":     "d13C_wood",
        "area_threshold":   0.5,
        "prob_threshold":   0.95,
        "seed":             1350,
    }
)
print("Job R run_assign:", job_assign_r.id)
run_assign_task.delay(job_assign_r.id)
```

### 6b. Engine Python

```python
job_assign_py = Job.objects.create(
    project_id=PROJECT_ID,
    created_by=user,
    job_type=Job.JobType.RUN_ASSIGN,
    status=Job.Status.PENDING,
    config={
        "execution_engine": "python",
        "isoscape_path":    f"/data/isoscapes/{job_iso_py.id}/isoscape_combined_py.tif",
        "unknown_path":     "/data/datasets/unknowns.csv",
        "regions_shp":      "/data/shapefiles/fu.shp",
        "regions_field":    "ADM1_PT",
        "regions_filter":   ["Amazonas", "Mato Grosso"],
        "response_col":     "d13C_wood",
        "area_threshold":   0.5,
        "prob_threshold":   0.95,
        "seed":             1350,
    }
)
print("Job Python run_assign:", job_assign_py.id)
run_assign_task.delay(job_assign_py.id)
```

Esperado em `/data/assignments/<job_id>/` (ou `/data/isoscapes/assignments/<job_id>/` se `DATA_ASSIGNMENTS_DIR` não estiver definido):
```
pd_map_103_r.tif (ou _py.tif)         ← um por amostra: probabilidade posterior por pixel
qtl_area_103_r.tif                    ← seleção dos top 50% pixels (threshold por área)
qtl_prob_103_r.tif                    ← seleção mínima de pixels somando ≥95% de probabilidade
posterior_probs_r.csv                 ← prob posterior agregada por região
odds_ratios_r.csv                     ← razão de chances entre pares de regiões
log.txt
metrics.json
```

### Verificar status de run_assign

```python
for _ in range(18):       # até 3 minutos (mais rápido que run_isoscape)
    job_assign_r.refresh_from_db()
    print(f"[R]  status={job_assign_r.status} | step={job_assign_r.progress_step}")
    if job_assign_r.status in ("completed", "failed"):
        break
    time.sleep(10)

if job_assign_r.status == "failed":
    print("ERRO R:", job_assign_r.error_message)
    print("LOG R:\n", job_assign_r.log[:3000])
```

### Inspecionar resultados de assign

```python
import json
import pandas as pd

m = json.load(open(f"/data/assignments/{job_assign_py.id}/metrics.json"))
print("Regiões:", m["regions"])
print("Médias do isoscape por região:")
for r, v in m["region_means"].items():
    print(f"  {r}: {v:.4f}")
print("Região mais provável por amostra:", m["most_likely"])

# Posteriores detalhados
post = pd.read_csv(m["posterior_csv"])
print("\nPosteriores por amostra/região:")
print(post.to_string(index=False))

# Odds ratios
odds = pd.read_csv(m["odds_csv"])
print("\nOdds ratios:")
print(odds.to_string(index=False))
```

---

## 7. Ver logs do worker em tempo real

```bash
docker compose logs -f worker
```

---

## 8. Debug manual (sem Django/Celery)

### gen_rasters

```bash
# Verificar se os .tif foram gerados (substitua PROJECT_ID)
docker compose exec worker bash -c 'ls -lh /data/rasters/1/amazonia_legal/*.tif 2>/dev/null || echo "NENHUM TIF ENCONTRADO"'

# Verificar o log
docker compose exec worker bash -c 'cat /data/rasters/1/amazonia_legal/log.txt'

# Verificar cache compartilhado (R e Python usam o mesmo)
docker compose exec worker bash -c 'ls -la /data/worldclim_cache/climate/'
docker compose exec worker bash -c 'ls -la /data/worldclim_cache/climate/wc2.1_10m/ | head -20'

# Rodar gen_rasters.R manualmente
docker compose exec worker Rscript r_scripts/gen_rasters.R \
    --job-id debug-r \
    --shapefile /data/shapefiles/amazonia_legal.shp \
    --output-dir /tmp/debug_rasters \
    --worldclim-dir /data/worldclim_cache \
    --variables tavg \
    --resolution 10 \
    --skip-existing FALSE

# Rodar gen_rasters.py manualmente (vai reaproveitar o que o R baixou)
docker compose exec worker python python_scripts/gen_rasters.py \
    --job-id debug-py \
    --shapefile /data/shapefiles/amazonia_legal.shp \
    --output-dir /tmp/debug_rasters \
    --worldclim-dir /data/worldclim_cache \
    --variables tavg \
    --resolution 10 \
    --skip-existing false
```

Os dois devem coexistir em `/tmp/debug_rasters/`:
```
amazonia_legal_10arc_tavg_mean_r.tif
amazonia_legal_10arc_tavg_mean_py.tif
```

### run_isoscape

```bash
# Rodar run_isoscape.R manualmente (só depois de ter .tif)
docker compose exec worker Rscript r_scripts/run_isoscape.R \
    --job-id debug-r \
    --dataset-path /data/datasets/madeiras.csv \
    --raster-dir /data/rasters/1/amazonia_legal \
    --output-dir /tmp/debug_iso_r \
    --response-col d13C_wood \
    --lat-col latitude \
    --lon-col longitude \
    --uncertainty quantile_rf \
    --resolution 10 \
    --seed 1350

# Rodar run_isoscape.py manualmente
docker compose exec worker python python_scripts/run_isoscape.py \
    --job-id debug-py \
    --dataset-path /data/datasets/madeiras.csv \
    --raster-dir /data/rasters/1/amazonia_legal \
    --output-dir /tmp/debug_iso_py \
    --response-col d13C_wood \
    --lat-col latitude \
    --lon-col longitude \
    --uncertainty quantile_rf \
    --resolution 10 \
    --seed 1350

# Forçar Python a usar rasters do R (cruzar engines):
docker compose exec worker python python_scripts/run_isoscape.py \
    --job-id debug-cross \
    --dataset-path /data/datasets/madeiras.csv \
    --raster-dir /data/rasters/1/amazonia_legal \
    --raster-suffix _r \
    --output-dir /tmp/debug_iso_cross \
    --response-col d13C_wood \
    --uncertainty quantile_rf \
    --resolution 10
```

### run_assign

```bash
# Rodar run_assign.R manualmente
docker compose exec worker Rscript r_scripts/run_assign.R \
    --job-id debug-r \
    --isoscape-path /data/isoscapes/<JOB_ISO_R_ID>/isoscape_combined_r.tif \
    --unknown-path /data/datasets/unknowns.csv \
    --regions-shp /data/shapefiles/fu.shp \
    --regions-field ADM1_PT \
    --regions-filter "Amazonas,Mato Grosso" \
    --output-dir /tmp/debug_assign_r \
    --response-col d13C_wood \
    --area-threshold 0.5 \
    --prob-threshold 0.95

# Rodar run_assign.py manualmente
docker compose exec worker python python_scripts/run_assign.py \
    --job-id debug-py \
    --isoscape-path /data/isoscapes/<JOB_ISO_PY_ID>/isoscape_combined_py.tif \
    --unknown-path /data/datasets/unknowns.csv \
    --regions-shp /data/shapefiles/fu.shp \
    --regions-field ADM1_PT \
    --regions-filter "Amazonas,Mato Grosso" \
    --output-dir /tmp/debug_assign_py \
    --response-col d13C_wood \
    --area-threshold 0.5 \
    --prob-threshold 0.95
```

---

## 9. Verificar outputs finais

```bash
# Rasters gerados (R + Python coexistem com sufixos diferentes)
docker compose exec worker bash -c 'ls -lh /data/rasters/1/amazonia_legal/'

# Isoscapes gerados
docker compose exec worker bash -c 'ls -lh /data/isoscapes/*/'

# Atribuições geradas
docker compose exec worker bash -c 'ls -lh /data/assignments/*/ 2>/dev/null || ls -lh /data/isoscapes/assignments/*/'

# metrics.json de um job (substitua JOB_ID)
docker compose exec worker bash -c 'cat /data/isoscapes/<JOB_ID>/metrics.json'

# Cache compartilhado: arquivos baixados uma vez, usados pelas duas engines
docker compose exec worker bash -c 'du -sh /data/worldclim_cache/climate/*'
```

---

## 10. Pipeline completo de teste em um bloco (copia-e-cola)

Para validar tudo de uma vez, no `make shell`:

```python
import time
from apps.jobs.models import Job
from apps.jobs.tasks import gen_rasters_task, run_isoscape_task, run_assign_task

PROJECT_ID, SA_ID = 1, 1   # ajuste

def wait(job, label, max_wait_min=10):
    for _ in range(max_wait_min * 6):
        job.refresh_from_db()
        print(f"[{label}] status={job.status} | step={job.progress_step}")
        if job.status in ("completed", "failed"):
            return
        time.sleep(10)

# ── 1. Gerar rasters com as DUAS engines ──────────────────────────────────────
common_gen = dict(
    project_id=PROJECT_ID, created_by=user,
    job_type=Job.JobType.GEN_RASTERS, status=Job.Status.PENDING,
)
cfg_gen = dict(
    study_area_id=SA_ID, study_area_name="amazonia_legal",
    shapefile_path="/data/shapefiles/amazonia_legal.shp",
    variables=["tavg"], resolution="10", skip_existing=True,
)
j_gen_r  = Job.objects.create(**common_gen, config={"execution_engine": "r",      **cfg_gen})
j_gen_py = Job.objects.create(**common_gen, config={"execution_engine": "python", **cfg_gen})
gen_rasters_task.delay(j_gen_r.id)
gen_rasters_task.delay(j_gen_py.id)
wait(j_gen_r,  "GEN_R")
wait(j_gen_py, "GEN_PY")

# ── 2. Rodar isoscape com as DUAS engines ─────────────────────────────────────
common_iso = dict(
    project_id=PROJECT_ID, created_by=user,
    job_type=Job.JobType.RUN_ISOSCAPE, status=Job.Status.PENDING,
)
cfg_iso = dict(
    dataset_path="/data/datasets/madeiras.csv",
    raster_dir=f"/data/rasters/{PROJECT_ID}/amazonia_legal",
    response_col="d13C_wood", lat_col="latitude", lon_col="longitude",
    uncertainty="quantile_rf", resolution="10", seed=1350,
)
j_iso_r  = Job.objects.create(**common_iso, config={"execution_engine": "r",      **cfg_iso})
j_iso_py = Job.objects.create(**common_iso, config={"execution_engine": "python", **cfg_iso})
run_isoscape_task.delay(j_iso_r.id)
run_isoscape_task.delay(j_iso_py.id)
wait(j_iso_r,  "ISO_R",  max_wait_min=10)
wait(j_iso_py, "ISO_PY", max_wait_min=10)

# ── 3. Rodar assign com as DUAS engines (precisa de unknowns.csv + fu.shp) ────
common_asg = dict(
    project_id=PROJECT_ID, created_by=user,
    job_type=Job.JobType.RUN_ASSIGN, status=Job.Status.PENDING,
)
cfg_asg = dict(
    unknown_path="/data/datasets/unknowns.csv",
    regions_shp="/data/shapefiles/fu.shp",
    regions_field="ADM1_PT",
    regions_filter=["Amazonas", "Mato Grosso"],
    response_col="d13C_wood",
    area_threshold=0.5, prob_threshold=0.95, seed=1350,
)
j_asg_r  = Job.objects.create(**common_asg, config={
    "execution_engine": "r",
    "isoscape_path": f"/data/isoscapes/{j_iso_r.id}/isoscape_combined_r.tif",
    **cfg_asg
})
j_asg_py = Job.objects.create(**common_asg, config={
    "execution_engine": "python",
    "isoscape_path": f"/data/isoscapes/{j_iso_py.id}/isoscape_combined_py.tif",
    **cfg_asg
})
run_assign_task.delay(j_asg_r.id)
run_assign_task.delay(j_asg_py.id)
wait(j_asg_r,  "ASG_R")
wait(j_asg_py, "ASG_PY")

print("\n✓ Pipeline completo concluído")
print(f"Gen R:   job {j_gen_r.id}  | status {j_gen_r.status}")
print(f"Gen Py:  job {j_gen_py.id} | status {j_gen_py.status}")
print(f"Iso R:   job {j_iso_r.id}  | status {j_iso_r.status}")
print(f"Iso Py:  job {j_iso_py.id} | status {j_iso_py.status}")
print(f"Asg R:   job {j_asg_r.id}  | status {j_asg_r.status}")
print(f"Asg Py:  job {j_asg_py.id} | status {j_asg_py.status}")
```

---

## Notas

| Item | Detalhe |
|---|---|
| Engine por job | Definida via `config.execution_engine`: `"r"` ou `"python"` |
| Sufixos de saída | R → `_r.tif`/`_r.csv` &middot; Python → `_py.tif`/`_py.csv` (coexistem no mesmo diretório) |
| Cache WorldClim | `/data/worldclim_cache/climate/wc2.1_{res}m/` — compartilhado entre R e Python via convenção do `geodata` |
| Filtro de rasters no `run_isoscape` | Por padrão, R lê `*_r.tif` e Python lê `*_py.tif`. Override com `raster_suffix` no config (`""` = todos) |
| `quantile-forest` (Python) | Equivalente direto do `ranger::ranger(..., quantreg=TRUE)`. Sem ele, Python cai para GBR quantile (fallback menos fiel) |
| Fallback automático | Se `SCRIPT_ENGINE_FALLBACK=True` no settings e o Python falhar, tenta R |
| Engine usada no log | O log do job começa com `[engine] r`, `[engine] python` ou `[engine] r (fallback)` |
| gen_rasters R trava | Quase sempre é timeout de rede no download do WorldClim ou falta de permissão em `worldclim_dir` — veja o log em `/data/rasters/<project_id>/<study_area_name>/log.txt` |
| Paridade VSURF no Python | `run_isoscape.py` usa um `VSURFApprox` próprio (3 etapas: threshold → interp → pred) com os mesmos hiperparâmetros do VSURF do R. Não é bit-idêntico ao VSURF original, mas converge para conjuntos de preditoras muito similares |
| RF principal | Ambos usam `ntree=2000` no modelo final + `ntree=500` na avaliação treino/teste (espelha `03_integracao_ML.R`) |
| Split treino/teste | R usa `caret::createDataPartition(p=0.8, strata=response)`; Python usa `StratifiedShuffleSplit` em quantis da resposta (equivalente) |
| Tempo esperado | gen_rasters: 2–10 min por variável (depende da banda larga). run_isoscape: 2–8 min (VSURF é o gargalo). run_assign: 30s–2 min |
| `run_assign` no banco | A task tenta criar um registro `apps.assignments.models.Assignment` se o app existir. Caso contrário, o resultado fica só nos arquivos + `metrics.json` |
| Adicionar `RUN_ASSIGN` ao enum | `Job.JobType.RUN_ASSIGN = "run_assign"` no model `Job` — necessário para a task ser disparada |