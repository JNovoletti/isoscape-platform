from django.db import models
from django.conf import settings
 
 
class Dataset(models.Model):
    class Source(models.TextChoices):
        UPLOAD = "upload", "Upload"
        API_ISOTOPESDB = "api_isotopesdb", "API isotopes.db"
 
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="datasets",
    )
    name = models.CharField(max_length=255)
    file_path = models.CharField(max_length=1024)
    source = models.CharField(
        max_length=30,
        choices=Source.choices,
        default=Source.UPLOAD,
    )
    row_count = models.PositiveIntegerField(null=True, blank=True)
    lat_column = models.CharField(max_length=100, blank=True)
    lon_column = models.CharField(max_length=100, blank=True)
    response_column = models.CharField(max_length=100, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
 
    class Meta:
        ordering = ["-uploaded_at"]
 
    def __str__(self):
        return f"{self.name} ({self.project})"
 