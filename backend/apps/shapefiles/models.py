from django.db import models
 
 
class StudyArea(models.Model):
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="study_areas",
        null=True,
        blank=True,
        help_text="Nulo quando for um shapefile pré-carregado pelo admin (preset).",
    )
    name = models.CharField(max_length=255)
    file_path = models.CharField(max_length=1024)
    crs = models.CharField(
        max_length=50,
        blank=True,
        help_text="Ex: EPSG:4674",
    )
    bbox = models.JSONField(
        null=True,
        blank=True,
        help_text="{ minx, miny, maxx, maxy }",
    )
    is_preset = models.BooleanField(
        default=False,
        help_text="True = shapefile pré-carregado pelo admin, disponível para todos.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
 
    class Meta:
        ordering = ["name"]
 
    def __str__(self):
        return self.name
 