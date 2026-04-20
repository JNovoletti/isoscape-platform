# 🔧 Troubleshooting: gen_rasters_task não executa

## Problema
Você cria um Job com `gen_rasters_task.delay(job.id)`, mas:
- ❌ Nenhum raster é gerado
- ❌ Nenhum arquivo WorldClim é baixado
- ❌ Sem erros visíveis no Django

## Árvore de Diagnóstico

```
gen_rasters_task.delay(job.id) chamado
    ↓
    ├─→ [1] Mensagem chega no Redis?
    │      └─→ NÃO: Redis não está rodando
    │
    ├─→ [2] Worker Celery processa a mensagem?
    │      └─→ NÃO: Worker não está rodando
    │
    ├─→ [3] Rscript está instalado no container?
    │      └─→ NÃO: Script R não executa
    │
    ├─→ [4] Script R consegue ler o shapefile?
    │      └─→ NÃO: Permissão ou arquivo não existe
    │
    └─→ [5] geodata::worldclim_global() consegue baixar?
           └─→ NÃO: Problema de internet ou dependências R
```

## Solução Passo a Passo

### PASSO 1: Verifique se Docker está rodando

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

**Se algum container está `Down`:**
```bash
docker-compose up -d
```

### PASSO 2: Verifique se Rscript está no container worker

```bash
docker-compose exec worker which Rscript
```

**Se retornar erro:**
Edite `backend/Dockerfile.worker` e adicione R. Exemplo:

```dockerfile
# Dockerfile.worker
FROM r-base:4.4

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# ... resto do arquivo
```

Depois:
```bash
docker-compose up -d --build worker
```

### PASSO 3: Teste o script R manualmente

```bash
docker-compose exec worker Rscript \
  /app/r_scripts/gen_rasters.R \
  --job-id "1" \
  --shapefile "/data/shapefiles/amazonia_legal.shp" \
  --output-dir "/tmp/test_rasters" \
  --worldclim-dir "/data/worldclim_cache" \
  --variables "tavg" \
  --resolution "10" \
  --skip-existing "TRUE"
```

Se der erro, corrija o problema específico (permissão, dependência R, etc).

### PASSO 4: Verifique os logs do worker

Em um terminal:
```bash
docker-compose logs -f worker
```

Dispare o job em outro terminal e veja o output em tempo real.

### PASSO 5: Teste com execução síncrona

No Django shell:
```python
# Ao invés de:
# result = gen_rasters_task.delay(job.id)

# Faça:
from apps.jobs.tasks import gen_rasters_task
try:
    gen_rasters_task(job.id)
    job.refresh_from_db()
    print(f"Status: {job.status}")
    print(f"Log: {job.log}")
except Exception as e:
    job.refresh_from_db()
    print(f"Erro: {job.error_message}")
    print(f"Log: {job.log}")
```

Isso vai mostrar o ERRO EXATO.

## Problemas Comuns

### ❌ "Rscript: command not found"
**Causa:** R não instalado no container worker
**Solução:**
```dockerfile
# Dockerfile.worker
FROM r-base:4.4  # Use uma imagem que já tenha R
```

### ❌ "Shapefile not found"
**Causa:** Caminho do shapefile incorreto ou não montado
**Solução:**
```bash
# Verifique se existe:
docker-compose exec worker ls -la /data/shapefiles/
```

### ❌ "geodata::worldclim_global() failed"
**Causa:** Dependências R faltando ou internet lenta
**Solução:**
```r
# No container, instale dependências:
docker-compose exec worker R
> install.packages(c("terra", "geodata", "optparse", "jsonlite"))
```

### ❌ Job fica em status RUNNING para sempre
**Causa:** Worker não conseguiu chamar Rscript e morreu silenciosamente
**Solução:**
```bash
# Verifique se o worker está vivo:
docker-compose logs worker | tail -100

# Reinicie:
docker-compose restart worker
```

### ❌ Erro de permissão em /data/rasters
**Causa:** Diretório criado com usuário root
**Solução:**
```bash
docker-compose exec worker chmod -R 777 /data/rasters
docker-compose exec worker chmod -R 777 /data/worldclim_cache
```

## Teste Completo (sem Docker)

Se você quer rodar localmente para testar:

```bash
cd /home/jnov/isoscape-platform/backend

# 1. Configure Python
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Instale R e pacotes
# (Ubuntu/Debian)
sudo apt-get install r-base r-base-dev
R
> install.packages(c("terra", "geodata", "optparse", "jsonlite"))

# 3. Configure Redis
# (Ubuntu/Debian)
sudo apt-get install redis-server
redis-server &

# 4. Execute o script R manualmente
Rscript r_scripts/gen_rasters.R \
  --job-id "1" \
  --shapefile "/data/shapefiles/amazonia_legal.shp" \
  --output-dir "/data/rasters/1/amazonia_legal" \
  --worldclim-dir "/data/worldclim_cache" \
  --variables "tavg" \
  --resolution "10" \
  --skip-existing "TRUE"

# 5. Verifique output
ls -la /data/rasters/1/amazonia_legal/
```

## Comando de Debug Úteis

```bash
# Ver logs do worker em tempo real
docker-compose logs -f worker

# Ver status do Redis
docker-compose exec redis redis-cli PING

# Ver tasks na fila
docker-compose exec redis redis-cli -n 0 KEYS "*"

# Limpar fila Redis (CUIDADO!)
docker-compose exec redis redis-cli FLUSHDB

# Entrar no shell do worker
docker-compose exec worker bash

# Testar conexão Django no worker
docker-compose exec worker python manage.py shell
```

## Próximos Passos

1. **Rode um dos testes acima**
2. **Copie a mensagem de erro**
3. **Cole aqui ou no GitHub Issues com:**
   - Output do `docker-compose logs worker`
   - Output do comando `Rscript` executado manualmente
   - Seu `docker-compose.yml`
   - Seu `Dockerfile.worker`
