from django.db import models
from django.conf import settings
 
 
class Job(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendente"
        RUNNING = "running", "Em execução"
        COMPLETED = "completed", "Concluído"
        FAILED = "failed", "Falhou"
 
    class JobType(models.TextChoices):
        PREPARE_DATASET = "prepare_dataset", "Pré-processamento de Dataset"
        GEN_RASTERS     = "gen_rasters",     "Geração de Rasters"
        RUN_ISOSCAPE    = "run_isoscape",    "Geração de Isoscape"
        RUN_ASSIGN      = "run_assign",      "Atribuição de Origem"
 
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="jobs",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="jobs",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    job_type = models.CharField(
        max_length=20,
        choices=JobType.choices,
    )
    celery_task_id = models.CharField(max_length=255, blank=True)
    config = models.JSONField(
        default=dict,
        help_text="Parâmetros do job: modelo, incerteza, resolução, variáveis, colunas.",
    )
    log = models.TextField(
        blank=True,
        help_text="Log acumulado do processo R.",
    )
    progress_step = models.CharField(
        max_length=255,
        blank=True,
        help_text="Ex: '3/7 — Fitting Random Forest'",
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
 
    class Meta:
        ordering = ["-created_at"]
 
    def __str__(self):
        return f"Job {self.id} — {self.get_job_type_display()} [{self.status}]"