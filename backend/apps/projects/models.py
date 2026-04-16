from django.db import models
from django.conf import settings
 
 
class Project(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="projects",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    isotope_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Ex: d13C, d18O, d2H",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
 
    class Meta:
        ordering = ["-created_at"]
 
    def __str__(self):
        return self.name
 