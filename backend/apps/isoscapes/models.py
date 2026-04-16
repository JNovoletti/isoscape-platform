from django.db import models
 
 
class Isoscape(models.Model):
    class ModelType(models.TextChoices):
        RANDOM_FOREST = "random_forest", "Random Forest"
        SPATIAL_RF = "spatial_rf", "Random Forest Espacial"
        GRADIENT_BOOSTING = "gradient_boosting", "Gradient Boosting"
        XGBOOST = "xgboost", "XGBoost"
        KRIGING_SIMPLE = "kriging_simple", "Krigagem Simples"
        KRIGING_UNIVERSAL = "kriging_universal", "Krigagem Universal"
        REGRESSION_KRIGING = "regression_kriging", "Regression-Kriging"
 
    class UncertaintyMethod(models.TextChoices):
        QUANTILE_RF = "quantile_rf", "Quantile RF"
        BOOTSTRAP = "bootstrap", "Bootstrap"
 
    job = models.OneToOneField(
        "jobs.Job",
        on_delete=models.CASCADE,
        related_name="isoscape",
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="isoscapes",
    )
    name = models.CharField(max_length=255)
    isoscape_path = models.CharField(max_length=1024)
    uncertainty_path = models.CharField(max_length=1024)
    mse = models.FloatField(null=True, blank=True)
    r_squared = models.FloatField(null=True, blank=True)
    selected_vars = models.JSONField(
        default=dict,
        help_text="{ threshold_vars, interp_vars, pred_vars } retornado pelo VSURF.",
    )
    model_type = models.CharField(
        max_length=30,
        choices=ModelType.choices,
        default=ModelType.RANDOM_FOREST,
    )
    uncertainty_method = models.CharField(
        max_length=20,
        choices=UncertaintyMethod.choices,
        default=UncertaintyMethod.QUANTILE_RF,
    )
    is_public = models.BooleanField(
        default=False,
        help_text="Permite visualização sem autenticação.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
 
    class Meta:
        ordering = ["-created_at"]
 
    def __str__(self):
        return f"{self.name} (R²={self.r_squared:.3f})" if self.r_squared else self.name
 