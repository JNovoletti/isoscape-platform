#!/usr/bin/env python
"""
Script de debug para testar o Celery e gen_rasters_task
Executa localmente sem precisar do worker para identificar o problema
"""

import os
import sys
import django
import subprocess
from pathlib import Path

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

from django.contrib.auth import get_user_model
from apps.projects.models import Project
from apps.shapefiles.models import StudyArea
from apps.jobs.models import Job
from apps.jobs.tasks import gen_rasters_task
from django.conf import settings

User = get_user_model()

print("=" * 80)
print("TEST 1: Verificar se Rscript está instalado")
print("=" * 80)
result = subprocess.run(["which", "Rscript"], capture_output=True, text=True)
if result.returncode == 0:
    print(f"✓ Rscript encontrado em: {result.stdout.strip()}")
else:
    print("✗ Rscript NÃO ENCONTRADO! Isso é o problema!")
    print("  → Instale R no sistema ou container")
    sys.exit(1)

print("\n" + "=" * 80)
print("TEST 2: Verificar diretórios de dados")
print("=" * 80)
print(f"DATA_RASTERS_DIR:    {settings.DATA_RASTERS_DIR}")
print(f"DATA_SHAPEFILES_DIR: {settings.DATA_SHAPEFILES_DIR}")
print(f"DATA_WORLDCLIM_DIR:  {settings.DATA_WORLDCLIM_DIR}")

for dir_path in [settings.DATA_RASTERS_DIR, settings.DATA_SHAPEFILES_DIR, settings.DATA_WORLDCLIM_DIR]:
    if dir_path.exists():
        print(f"✓ {dir_path} existe")
    else:
        print(f"✗ {dir_path} NÃO existe!")
        dir_path.mkdir(parents=True, exist_ok=True)
        print(f"  → Criado")

print("\n" + "=" * 80)
print("TEST 3: Verificar shapefile amazonia_legal")
print("=" * 80)
shp_path = Path("/data/shapefiles/amazonia_legal.shp")
if shp_path.exists():
    print(f"✓ Shapefile encontrado: {shp_path}")
else:
    print(f"✗ Shapefile NÃO encontrado: {shp_path}")
    sys.exit(1)

print("\n" + "=" * 80)
print("TEST 4: Criar Job de teste")
print("=" * 80)
# Limpar dados antigos de teste
User.objects.filter(username="teste_debug").delete()

user = User.objects.create_user(
    username="teste_debug",
    email="teste_debug@example.com",
    password="123456"
)
print(f"✓ Usuário criado: {user.username}")

project = Project.objects.create(
    owner=user,
    name="Teste Debug Gen Rasters",
    isotope_type="d13C"
)
print(f"✓ Projeto criado: {project.name} (ID: {project.id})")

sa, created = StudyArea.objects.get_or_create(
    name="amazonia_legal",
    defaults={
        "file_path": "/data/shapefiles/amazonia_legal.shp",
        "is_preset": True,
    }
)
print(f"{'✓' if created else '→'} StudyArea obtida: {sa.name} (ID: {sa.id})")

job = Job.objects.create(
    project=project,
    created_by=user,
    job_type=Job.JobType.GEN_RASTERS,
    config={
        "study_area_id": sa.id,
        "study_area_name": sa.name,
        "shapefile_path": str(sa.file_path),
        "variables": ["tavg"],  # Apenas 1 variável para teste rápido
        "resolution": "10",
        "skip_existing": True,
    }
)
print(f"✓ Job criado: ID {job.id}")

print("\n" + "=" * 80)
print("TEST 5: Executar gen_rasters_task LOCALMENTE (sem Celery)")
print("=" * 80)
print(f"Isso é um teste de execução síncrona do script R...")
print()

try:
    # Chamar a função diretamente (sem .delay())
    gen_rasters_task(job.id)
    print("✓ Tarefa executada com sucesso!")
    
    # Recarregar job
    job.refresh_from_db()
    print(f"  Job status: {job.status}")
    print(f"  Job log:\n{job.log}")
    
except Exception as e:
    print(f"✗ Erro ao executar tarefa: {e}")
    job.refresh_from_db()
    print(f"  Job status: {job.status}")
    print(f"  Job error_message: {job.error_message}")
    print(f"  Job log:\n{job.log}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 80)
print("TEST 6: Verificar se rasters foram gerados")
print("=" * 80)
output_dir = settings.DATA_RASTERS_DIR / str(project.id) / sa.name
print(f"Procurando arquivos em: {output_dir}")
if output_dir.exists():
    files = list(output_dir.glob("*.tif"))
    if files:
        print(f"✓ {len(files)} arquivo(s) .tif encontrado(s):")
        for f in files:
            print(f"  - {f.name}")
    else:
        print("✗ Nenhum arquivo .tif encontrado!")
    
    metrics_file = output_dir / "metrics.json"
    if metrics_file.exists():
        print(f"✓ metrics.json encontrado")
        import json
        metrics = json.loads(metrics_file.read_text())
        print(f"  generated_files: {metrics.get('generated_files', [])}")
        print(f"  failed_vars: {metrics.get('failed_vars', [])}")
else:
    print(f"✗ Diretório não existe: {output_dir}")

print("\n" + "=" * 80)
print("RESUMO")
print("=" * 80)
print("Se tudo passou, o problema é:")
print("  1. Worker Celery não está rodando")
print("  2. Redis não está acessível")
print("  3. Variáveis de ambiente incorretas no container worker")
print()
print("Comando para checar worker:")
print("  docker-compose logs -f worker")
print()
print("Comando para reiniciar tudo:")
print("  docker-compose down && docker-compose up -d")
