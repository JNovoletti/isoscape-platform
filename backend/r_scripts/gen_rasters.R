#!/usr/bin/env Rscript
# =============================================================================
# gen_rasters.R
# Baixa dados do WorldClim, recorta pelo shapefile e salva os .tif.
# Chamado pelo worker Celery antes de run_isoscape.R.
#
# Uso:
#   Rscript gen_rasters.R \
#     --job-id        abc-123 \
#     --shapefile     /data/shapefiles/amazonia_legal.shp \
#     --output-dir    /data/rasters/project1/ \
#     --worldclim-dir /data/worldclim_cache/ \
#     --variables     tavg,tmax,tmin,prec \
#     --bio-layers    bio1,bio2,bio3 \
#     --resolution    5 \
#     --skip-existing TRUE
# =============================================================================

suppressPackageStartupMessages({
  library(optparse)
  library(terra)
  library(geodata)
  library(jsonlite)
})

# ── CLI arguments ─────────────────────────────────────────────────────────────
option_list <- list(
  make_option("--job-id",        type = "character", help = "ID do Job"),
  make_option("--shapefile",     type = "character", help = "Caminho do .shp da área de estudo"),
  make_option("--output-dir",    type = "character", help = "Diretório de saída dos .tif recortados"),
  make_option("--worldclim-dir", type = "character", help = "Diretório de cache do WorldClim"),
  make_option("--variables",     type = "character", help = "Variáveis separadas por vírgula. Ex: tavg,tmax,bio"),
  make_option("--bio-layers",    type = "character", default = "",
              help = "Bioclim layers a manter, separados por vírgula. Ex: bio1,bio2,bio12. Vazio = todos"),
  make_option("--resolution",    type = "character", default = "5",
              help = "Resolução em arc-min: 2.5 | 5 | 10"),
  make_option("--skip-existing", type = "character", default = "TRUE",
              help = "Pular variáveis já processadas: TRUE | FALSE")
)

opt <- parse_args(OptionParser(option_list = option_list))

required <- c("job-id", "shapefile", "output-dir", "worldclim-dir", "variables")
missing  <- required[sapply(required, function(x) is.null(opt[[x]]))]
if (length(missing) > 0) stop("Argumentos obrigatórios ausentes: ", paste(missing, collapse = ", "))

job_id        <- opt[["job-id"]]
shp_path      <- opt[["shapefile"]]
output_dir    <- opt[["output-dir"]]
worldclim_dir <- opt[["worldclim-dir"]]
variables     <- trimws(strsplit(opt[["variables"]], ",")[[1]])
bio_layers    <- if (nchar(opt[["bio-layers"]]) > 0) trimws(strsplit(opt[["bio-layers"]], ",")[[1]]) else paste0("bio", 1:19)
resolution    <- as.numeric(opt[["resolution"]])
skip_existing <- toupper(opt[["skip-existing"]]) == "TRUE"

# ── Helpers de log ─────────────────────────────────────────────────────────────
log_path <- NULL

log_msg <- function(msg) {
  line <- paste0("[", format(Sys.time(), "%H:%M:%S"), "] ", msg)
  cat(line, "\n", sep = "")
  if (!is.null(log_path)) cat(line, "\n", sep = "", file = log_path, append = TRUE)
}

# ── Criar diretórios ───────────────────────────────────────────────────────────
if (!dir.exists(output_dir))    dir.create(output_dir,    recursive = TRUE)
if (!dir.exists(worldclim_dir)) dir.create(worldclim_dir, recursive = TRUE)
log_path <- file.path(output_dir, "log.txt")

log_msg(paste("[→] Job gen_rasters iniciado:", job_id))
log_msg(paste("[→] Shapefile:", shp_path))
log_msg(paste("[→] Variáveis:", paste(variables, collapse = ", ")))
log_msg(paste("[→] Resolução:", resolution, "arc-min"))
log_msg(paste("[→] Skip existing:", skip_existing))

# ── Carregar shapefile ─────────────────────────────────────────────────────────
log_msg("[→] Carregando shapefile...")
area <- tryCatch(
  terra::vect(shp_path),
  error = function(e) stop("Erro ao carregar shapefile: ", e$message)
)
log_msg(paste("[✓] Shapefile carregado:", basename(shp_path)))

# ── Prefixo para nomear os arquivos de saída ───────────────────────────────────
# Mesmo esquema do app.R: {prefix}_{res}arc_{var}_mean.tif ou {prefix}_{res}arc_bio{N}.tif
prefix  <- gsub("[^A-Za-z0-9_]", "_", tools::file_path_sans_ext(basename(shp_path)))
res_tag <- paste0(resolution, "arc")

# Guarda quais arquivos foram gerados para o metrics.json
generated_files <- character(0)
skipped_files   <- character(0)
failed_vars     <- character(0)

# ── Processar cada variável ────────────────────────────────────────────────────
for (var in variables) {

  if (var == "bio") {
    # ── Bioclim (bio1–bio19) ────────────────────────────────────────────────
    log_msg("[→] Baixando: bio (WorldClim bioclim)")

    bio_data <- tryCatch(
      geodata::worldclim_global(var = "bio", res = resolution, path = worldclim_dir),
      error = function(e) { log_msg(paste("[!] Erro ao baixar bio:", e$message)); NULL }
    )
    if (is.null(bio_data)) { failed_vars <- c(failed_vars, "bio"); next }

    bio_crop   <- terra::crop(bio_data, area)
    bio_masked <- terra::mask(bio_crop, area)
    bio_sirgas <- terra::project(bio_masked, "EPSG:4674")

    # Normaliza nomes das camadas para bio1, bio2, …, bio19
    layer_names <- names(bio_sirgas)
    bio_nums    <- gsub("bio_?0*", "bio", regmatches(layer_names, regexpr("bio_?0*([0-9]+)$", layer_names)))

    for (j in seq_along(bio_nums)) {
      bname <- bio_nums[j]
      if (!bname %in% bio_layers) next

      out_file <- file.path(output_dir, paste0(prefix, "_", res_tag, "_", bname, ".tif"))

      if (skip_existing && file.exists(out_file)) {
        log_msg(paste("  [skip]", basename(out_file)))
        skipped_files <- c(skipped_files, out_file)
        next
      }

      tryCatch({
        terra::writeRaster(bio_sirgas[[j]], filename = out_file, overwrite = TRUE)
        log_msg(paste("  [✓] Salvo:", basename(out_file)))
        generated_files <- c(generated_files, out_file)
      }, error = function(e) {
        log_msg(paste("  [!] Erro ao salvar", bname, ":", e$message))
        failed_vars <<- c(failed_vars, bname)
      })
    }

  } else {
    # ── Variáveis climáticas simples (tavg, tmax, tmin, prec, srad, vapr, wind, elev) ──
    out_file <- file.path(output_dir, paste0(prefix, "_", res_tag, "_", var, "_mean.tif"))

    if (skip_existing && file.exists(out_file)) {
      log_msg(paste("[skip]", basename(out_file)))
      skipped_files <- c(skipped_files, out_file)
      next
    }

    log_msg(paste("[→] Baixando:", var))
    climate_data <- tryCatch(
      geodata::worldclim_global(var = var, res = resolution, path = worldclim_dir),
      error = function(e) { log_msg(paste("  [!] Erro ao baixar", var, ":", e$message)); NULL }
    )
    if (is.null(climate_data)) { failed_vars <- c(failed_vars, var); next }

    r_crop   <- terra::crop(climate_data, area)
    r_masked <- terra::mask(r_crop, area)
    r_sirgas <- terra::project(r_masked, "EPSG:4674")

    # Variáveis com múltiplas camadas (ex: tavg tem 12 meses) → média
    r_out <- if (terra::nlyr(r_sirgas) > 1) terra::app(r_sirgas, mean) else r_sirgas

    tryCatch({
      terra::writeRaster(r_out, filename = out_file, overwrite = TRUE)
      log_msg(paste("[✓] Salvo:", basename(out_file)))
      generated_files <- c(generated_files, out_file)
    }, error = function(e) {
      log_msg(paste("[!] Erro ao salvar", var, ":", e$message))
      failed_vars <- c(failed_vars, var)
    })
  }
}

# ── Salvar metrics.json ────────────────────────────────────────────────────────
metrics <- list(
  job_id          = job_id,
  generated_files = generated_files,
  skipped_files   = skipped_files,
  failed_vars     = failed_vars,
  output_dir      = output_dir
)

metrics_path <- file.path(output_dir, "metrics.json")
write(jsonlite::toJSON(metrics, auto_unbox = TRUE, pretty = TRUE), metrics_path)
log_msg(paste("[✓] metrics.json salvo:", metrics_path))

if (length(failed_vars) > 0) {
  log_msg(paste("[!] Variáveis com falha:", paste(failed_vars, collapse = ", ")))
  quit(status = 1)
}

log_msg(paste("[★] gen_rasters concluído:", format(Sys.time(), "%Y-%m-%d %H:%M:%S")))