#!/usr/bin/env python
"""
Script simples para testar gen_rasters_task localmente
Não precisa de Docker, apenas do ambiente local configurado
"""

import os
import sys
import subprocess
from pathlib import Path

# Adicionar diretório do backend ao path
BACKEND_DIR = Path(__file__).parent
sys.path.insert(0, str(BACKEND_DIR))

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

import django
django.setup()

from django.contrib.auth import get_user_model
from apps.projects.models import Project
from apps.shapefiles.models import StudyArea
from apps.jobs.models import Job
from apps.jobs.tasks import gen_rasters_task
from django.conf import settings

def main():
    print("\n" + "=" * 80)
    print("TEST: Executar gen_rasters_task LOCALMENTE")
    print("=" * 80 + "\n")
    
    User = get_user_model()
    
    # 1. Criar usuário
    print("[1] Criando usuário de teste...")
    user, _ = User.objects.get_or_create(
        username="teste_local",
        defaults={"email": "teste@local.com"}
    )
    print(f"    ✓ Usuário: {user.username}")
    
    # 2. Criar projeto
    print("[2] Criando projeto...")
    project, _ = Project.objects.get_or_create(
        owner=user,
        name="Teste Local Gen Rasters",
        defaults={"isotope_type": "d13C"}
    )
    print(f"    ✓ Projeto: {project.name} (ID: {project.id})")
    
    # 3. Criar study area
    print("[3] Criando study area...")
    sa, _ = StudyArea.objects.get_or_create(
        name="amazonia_legal",
        defaults={
            "file_path": "/data/shapefiles/amazonia_legal.shp",
            "is_preset": True,
        }
    )
    print(f"    ✓ StudyArea: {sa.name} (ID: {sa.id})")
    
    # 4. Criar job
    print("[4] Criando job...")
    job = Job.objects.create(
        project=project,
        created_by=user,
        job_type=Job.JobType.GEN_RASTERS,
        config={
            "study_area_id": sa.id,
            "study_area_name": sa.name,
            "shapefile_path": str(sa.file_path),
            "variables": ["tavg"],  # Apenas tavg para teste rápido
            "resolution": "10",
            "skip_existing": True,
        }
    )
    print(f"    ✓ Job criado (ID: {job.id})")
    
    # 5. Executar tarefa localmente (SÍNCRONO)
    print("\n[5] Executando gen_rasters_task...")
    print("    (isso pode levar 1-5 minutos na primeira execução)\n")
    
    try:
        # Executar SEM Celery (direto)
        gen_rasters_task(job.id)
        
        # Recarregar job
        job.refresh_from_db()
        
        print(f"\n    ✓ Tarefa completada!")
        print(f"    Status: {job.status}")
        
        if job.status == Job.Status.COMPLETED:
            print(f"    ✓ Job SUCESSO!")
            
            # Listar arquivos gerados
            output_dir = settings.DATA_RASTERS_DIR / str(project.id) / sa.name
            if output_dir.exists():
                files = list(output_dir.glob("*.tif"))
                print(f"\n    Rasters gerados ({len(files)} arquivo(s)):")
                for f in files:
                    print(f"      - {f.name}")
            
            return 0
        else:
            print(f"    ✗ Job falhou")
            print(f"    Erro: {job.error_message}")
            print(f"\n    Log:\n{job.log}")
            return 1
            
    except Exception as e:
        job.refresh_from_db()
        print(f"\n    ✗ Erro ao executar: {e}")
        print(f"\n    Job status: {job.status}")
        print(f"    Job error: {job.error_message}")
        print(f"\n    Log:\n{job.log}")
        
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
