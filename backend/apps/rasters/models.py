from django.db import models
 
 
class RasterLayer(models.Model):
    class Resolution(models.TextChoices):
        RES_2_5 = "2.5", "2.5 arc-min"
        RES_5 = "5", "5 arc-min"
        RES_10 = "10", "10 arc-min"
 
    study_area = models.ForeignKey(
        "shapefiles.StudyArea",
        on_delete=models.CASCADE,
        related_name="raster_layers",
    )
    variable = models.CharField(
        max_length=50,
        help_text="Ex: tavg, tmin, prec, bio1 … bio19",
    )
    resolution = models.CharField(
        max_length=5,
        choices=Resolution.choices,
    )
    file_path = models.CharField(max_length=1024)
    generated_at = models.DateTimeField(auto_now_add=True)
 
    class Meta:
        ordering = ["variable"]
        unique_together = [("study_area", "variable", "resolution")]
 
    def __str__(self):
        return f"{self.study_area.name} — {self.variable} ({self.resolution} arc-min)"