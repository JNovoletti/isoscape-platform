🔍 CHECKLIST DE DIAGNÓSTICO
═══════════════════════════════════════════════════════════════════════════════

Marque cada item conforme você verificar:

## INFRAESTRUTURA

□ Docker está instalado?
  $ docker --version
  $ docker-compose --version

□ Docker Compose rodando?
  $ cd /home/jnov/isoscape-platform
  $ docker-compose ps
  
  Esperado:
    NAME                 STATUS
    isoscape_db          Up
    isoscape_redis       Up
    isoscape_backend     Up
    isoscape_worker      Up

□ Se algum está DOWN:
  $ docker-compose up -d

□ Redis acessível?
  $ docker-compose exec redis redis-cli PING
  Esperado: PONG

□ Banco de dados funciona?
  $ python manage.py shell
  >>> from django.contrib.auth import get_user_model
  >>> User = get_user_model()
  >>> User.objects.count()
  >>> 

□ Shapefile existe?
  $ docker-compose exec worker ls -la /data/shapefiles/amazonia_legal.*
  
  Esperado: 4+ arquivos (.shp, .dbf, .shx, .prj)

□ Rscript instalado no worker?
  $ docker-compose exec worker which Rscript
  
  Esperado: /usr/bin/Rscript (ou similar)

□ Pacotes R necessários instalados?
  $ docker-compose exec worker R
  > library(terra)
  > library(geodata)
  > library(optparse)
  > library(jsonlite)
  > q()
  
  Se algum falhar, o container precisa ser rebuiltado

## TESTE BÁSICO

□ Criar dados de teste (no Django shell):
  
  from django.contrib.auth import get_user_model
  from apps.projects.models import Project
  from apps.shapefiles.models import StudyArea
  from apps.jobs.models import Job
  
  User = get_user_model()
  user = User.objects.create_user(username="teste", email="t@t.com", password="123")
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
  
□ Observar os logs do worker (em outro terminal):
  $ docker-compose logs -f worker

□ Disparar a tarefa (no Django shell):
  from apps.jobs.tasks import gen_rasters_task
  result = gen_rasters_task.delay(job.id)
  print(f"Task ID: {result.id}")
  print(f"Job ID: {job.id}")

□ Verifique o que aparece nos logs do worker:
  
  Esperado:
    [tasks]
      . apps.jobs.tasks.gen_rasters_task
    
    Received task: apps.jobs.tasks.gen_rasters_task[...]
    Started gen_rasters...

  Se aparecer "Received task" → Worker está recebendo
  Se NÃO aparecer → Worker não está conectado ao Redis

□ Aguarde a tarefa terminar (pode levar 2-10 minutos)

□ Verifique o status do job:
  job.refresh_from_db()
  print(job.status)         # Deve ser 'COMPLETED' ou 'FAILED'
  print(job.log)            # Deve ter output do script R
  print(job.error_message)  # Se falhou

□ Verifique se rasters foram gerados:
  $ docker-compose exec worker ls /data/rasters/*/amazonia_legal/*.tif
  
  Esperado:
    /data/rasters/1/amazonia_legal/amazonia_legal_10arc_tavg_mean.tif

## TESTE SEM CELERY (debug)

□ Execute o teste local:
  $ cd /home/jnov/isoscape-platform/backend
  $ python test_gen_rasters_local.py

  Se funcionar:
    ✓ Script R está OK
    ✗ Problema é Celery/Redis

  Se falhar:
    ✓ Veja a mensagem de erro
    Procure em TROUBLESHOOTING_CELERY.md

## RESOLUÇÃO DE PROBLEMAS

PROBLEMA: "redis://redis:6379/0 does not seem to be running"
SOLUÇÃO:
  1. $ docker-compose ps | grep redis
  2. Se está DOWN: $ docker-compose up -d redis
  3. Se está UP: $ docker-compose restart redis
  4. Verifique: $ docker-compose exec redis redis-cli PING

---

PROBLEMA: "Rscript: command not found"
SOLUÇÃO:
  1. Editar backend/Dockerfile.worker
  2. Garantir que a base é FROM rocker/r-ver:4.4
  3. Rebuildar: $ docker-compose up -d --build worker

---

PROBLEMA: Job.status fica 'RUNNING' para sempre
SOLUÇÃO:
  1. Verifique os logs: $ docker-compose logs worker
  2. Se vê erro → corrija o problema
  3. Se não vê nada → worker morreu
  4. Reinicie: $ docker-compose restart worker

---

PROBLEMA: "No such file or directory: /data/shapefiles/amazonia_legal.shp"
SOLUÇÃO:
  1. Copiar shapefile para /data/shapefiles/
  2. Verificar: $ docker-compose exec worker ls /data/shapefiles/

---

PROBLEMA: Script R baixa dados mas não salva rasters
SOLUÇÃO:
  1. Verificar logs: job.log
  2. Verificar: $ docker-compose exec worker ls /data/rasters/
  3. Verificar permissões: $ docker-compose exec worker chmod 777 /data/rasters

═══════════════════════════════════════════════════════════════════════════════

PRÓXIMO PASSO: Comece do topo e marque cada item conforme completa!
