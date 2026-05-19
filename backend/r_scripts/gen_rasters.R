#!/usr/bin/env Rscript
# =============================================================================
# gen_rasters.R
# Equivalente backend do 01_worldclim.R do curso.
# Baixa dados do WorldClim, recorta pelo shapefile e salva os .tif.
# Chamado pelo worker Celery antes de run_isoscape.R.
#
# Cache compartilhado com gen_rasters.py:
#   {worldclim_dir}/climate/wc2.1_{res}m/wc2.1_{res}m_{var}_{NN}.tif
# (mesma estrutura do geodata, portanto Python e R reaproveitam downloads)
#
# Sufixo "_r" no nome do arquivo de saída diferencia da versão Python ("_py"):
#   {prefix}_{res}arc_{var}_mean_r.tif      ← R
#   {prefix}_{res}arc_{var}_mean_py.tif     ← Python
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

# ── Criar diretórios ───────────────────────────────────────────────────────────
if (!dir.exists(output_dir))    dir.create(output_dir,    recursive = TRUE)
if (!dir.exists(worldclim_dir)) dir.create(worldclim_dir, recursive = TRUE)

log_path <- file.path(output_dir, "log.txt")

# ── Helpers de log ─────────────────────────────────────────────────────────────
log_msg <- function(msg) {
  line <- paste0("[", format(Sys.time(), "%H:%M:%S"), "] ", msg)
  cat(line, "\n", sep = "")
  cat(line, "\n", sep = "", file = log_path, append = TRUE)
}

log_msg(paste("[→] Job gen_rasters iniciado:", job_id))
log_msg(paste("[→] Shapefile:", shp_path))
log_msg(paste("[→] Variáveis:", paste(variables, collapse = ", ")))
log_msg(paste("[→] Resolução:", resolution, "arc-min"))
log_msg(paste("[→] Skip existing:", skip_existing))
log_msg(paste("[→] WorldClim cache dir:", worldclim_dir))
log_msg(paste("[→] Output dir:", output_dir))
log_msg("[→] Engine: R (gera arquivos com sufixo '_r')")

# ── Diagnóstico de permissões ──────────────────────────────────────────────────
log_msg("[→] Verificando permissões de diretórios...")

check_dir <- function(path, label) {
  if (!dir.exists(path)) {
    log_msg(paste("[!] Diretório não existe:", label, "->", path))
    return(FALSE)
  }
  test_file <- file.path(path, paste0(".write_test_", Sys.getpid()))
  ok <- tryCatch({
    writeLines("test", test_file)
    file.remove(test_file)
    TRUE
  }, error = function(e) {
    log_msg(paste("[!] Sem permissão de escrita em", label, ":", e$message))
    FALSE
  })
  if (ok) log_msg(paste("[✓]", label, "OK →", path))
  ok
}

if (!check_dir(output_dir,    "output-dir"))    quit(status = 1)
if (!check_dir(worldclim_dir, "worldclim-dir")) quit(status = 1)

# ── Verificar shapefile ────────────────────────────────────────────────────────
log_msg(paste("[→] Verificando shapefile:", shp_path))
if (!file.exists(shp_path)) {
  log_msg(paste("[!] ERRO: shapefile não encontrado:", shp_path))
  quit(status = 1)
}

# ── Carregar shapefile ─────────────────────────────────────────────────────────
log_msg("[→] Carregando shapefile...")
area <- tryCatch(
  terra::vect(shp_path),
  error = function(e) {
    log_msg(paste("[!] ERRO ao carregar shapefile:", e$message))
    NULL
  }
)
if (is.null(area)) quit(status = 1)

log_msg(paste("[✓] Shapefile carregado:", basename(shp_path)))
log_msg(paste("[→] Extensão da área:", paste(round(as.vector(ext(area)), 4), collapse = " ")))
log_msg(paste("[→] CRS do shapefile:", crs(area, describe = TRUE)$code))
log_msg(paste("[→] Área (km²):", round(sum(terra::expanse(area, unit = "km")), 2)))

# ── Garantir que o geodata use o cache dir correto ─────────────────────────────
# O geodata salva em: {worldclim_dir}/climate/wc2.1_{res}m/
# Python usa exatamente o mesmo caminho — downloads são compartilhados.
options(geodata_path = worldclim_dir)
log_msg(paste("[→] geodata_path setado para:", worldclim_dir))
log_msg(paste("[→] Cache geodata esperado em:",
              file.path(worldclim_dir, "climate", paste0("wc2.1_", resolution, "m"))))

# ── Prefixo para nomear os arquivos de saída ───────────────────────────────────
prefix  <- gsub("[^A-Za-z0-9_]", "_", tools::file_path_sans_ext(basename(shp_path)))
res_tag <- paste0(resolution, "arc")

generated_files <- character(0)
skipped_files   <- character(0)
failed_vars     <- character(0)

# ── Helper: reprojetar área, recortar, mascarar e reprojetar raster ────────────
#
# Equivalente a:
#   area_reproj <- project(area_vect, crs(r))
#   r_crop      <- crop(r, area_reproj)
#   r_masked    <- mask(r_crop, area_reproj)
#   project(r_masked, target_crs)
#
# Espelha exatamente o fluxo do 01_worldclim.R do curso.
process_raster <- function(r, area_vect, target_crs = "EPSG:4674") {
  area_reproj <- tryCatch(
    terra::project(area_vect, terra::crs(r)),
    error = function(e) area_vect
  )
  r_crop   <- terra::crop(r, area_reproj)
  r_masked <- terra::mask(r_crop, area_reproj)
  terra::project(r_masked, target_crs)
}

# ── Processar cada variável ────────────────────────────────────────────────────
for (var in variables) {

  if (var == "bio") {
    # ── Bioclim (bio1–bio19) ────────────────────────────────────────────────
    out_file_check <- file.path(output_dir, paste0(prefix, "_", res_tag, "_bio1_r.tif"))
    if (skip_existing && file.exists(out_file_check)) {
      log_msg("[skip] Layers bio já existem — pulando download")
      for (bname in bio_layers) {
        f <- file.path(output_dir, paste0(prefix, "_", res_tag, "_", bname, "_r.tif"))
        if (file.exists(f)) skipped_files <- c(skipped_files, f)
      }
      next
    }

    log_msg("[→] Baixando: bio (WorldClim bioclim)")
    log_msg("[→] Aguardando resposta do WorldClim... (pode levar minutos)")

    bio_data <- tryCatch({
      geodata::worldclim_global(var = "bio", res = resolution, path = worldclim_dir)
    }, error = function(e) {
      log_msg(paste("[!] ERRO ao baixar bio:", e$message))
      NULL
    })

    if (is.null(bio_data)) {
      log_msg("[!] Download de bio falhou. Verifique conectividade e permissões do worldclim-dir.")
      failed_vars <- c(failed_vars, "bio")
      next
    }

    log_msg(paste("[✓] bio baixado:", terra::nlyr(bio_data), "camadas"))
    log_msg("[→] Recortando e reprojetando bio...")

    bio_proc <- tryCatch(
      process_raster(bio_data, area),
      error = function(e) {
        log_msg(paste("[!] ERRO ao processar bio:", e$message))
        NULL
      }
    )
    if (is.null(bio_proc)) { failed_vars <- c(failed_vars, "bio"); next }

    # Normalizar nomes das camadas (ex: wc2.1_bio_1 → bio1)
    layer_names <- names(bio_proc)
    bio_nums    <- gsub(".*bio_?0*([0-9]+).*", "bio\\1", layer_names)

    for (j in seq_along(bio_nums)) {
      bname <- bio_nums[j]
      if (!bname %in% bio_layers) next

      out_file <- file.path(output_dir, paste0(prefix, "_", res_tag, "_", bname, "_r.tif"))

      if (skip_existing && file.exists(out_file)) {
        log_msg(paste("  [skip]", basename(out_file)))
        skipped_files <- c(skipped_files, out_file)
        next
      }

      tryCatch({
        terra::writeRaster(bio_proc[[j]], filename = out_file, overwrite = TRUE)
        r_tmp <- terra::rast(out_file)
        log_msg(paste("  [✓] Salvo:", basename(out_file),
                      "| dimensões:", nrow(r_tmp), "x", ncol(r_tmp),
                      "| valores: [",
                      round(terra::global(r_tmp, "min", na.rm = TRUE)[[1]], 2),
                      ";",
                      round(terra::global(r_tmp, "max", na.rm = TRUE)[[1]], 2), "]"))
        generated_files <- c(generated_files, out_file)
      }, error = function(e) {
        log_msg(paste("  [!] Erro ao salvar", bname, ":", e$message))
        failed_vars <<- c(failed_vars, bname)
      })
    }

  } else {
    # ── Variáveis climáticas simples (tavg, tmax, tmin, prec, srad, vapr, wind, elev) ────
    out_file <- file.path(output_dir, paste0(prefix, "_", res_tag, "_", var, "_mean_r.tif"))

    if (skip_existing && file.exists(out_file)) {
      log_msg(paste("[skip]", basename(out_file)))
      skipped_files <- c(skipped_files, out_file)
      next
    }

    log_msg(paste("[→] Baixando:", var, "(res =", resolution, "arc-min)"))
    log_msg("[→] Aguardando resposta do WorldClim... (pode levar minutos)")

    # elev é tratado pelo elevation_global, não worldclim_global
    climate_data <- tryCatch({
      if (var == "elev") {
        geodata::elevation_global(res = resolution, path = worldclim_dir)
      } else {
        geodata::worldclim_global(var = var, res = resolution, path = worldclim_dir)
      }
    }, error = function(e) {
      log_msg(paste("[!] ERRO ao baixar", var, ":", e$message))
      NULL
    })

    if (is.null(climate_data)) {
      log_msg(paste("[!] Download de", var, "falhou."))
      failed_vars <- c(failed_vars, var)
      next
    }

    log_msg(paste("[✓]", var, "baixado:", terra::nlyr(climate_data), "camada(s)"))
    log_msg(paste("[→] Recortando e reprojetando", var, "..."))

    r_proc <- tryCatch(
      process_raster(climate_data, area),
      error = function(e) {
        log_msg(paste("[!] ERRO ao processar", var, ":", e$message))
        NULL
      }
    )
    if (is.null(r_proc)) { failed_vars <- c(failed_vars, var); next }

    # Múltiplas camadas (ex: 12 meses) → média (na.rm=TRUE descarta nodata)
    # Espelha terra::app(climate_amz_sirgas, mean) do 01_worldclim.R
    r_out <- if (terra::nlyr(r_proc) > 1) {
      log_msg(paste("[→]", var, "tem", terra::nlyr(r_proc), "camadas — calculando média anual"))
      terra::app(r_proc, mean, na.rm = TRUE)
    } else {
      r_proc
    }

    tryCatch({
      terra::writeRaster(r_out, filename = out_file, overwrite = TRUE)
      log_msg(paste("[✓] Salvo:", basename(out_file),
                    "| dimensões:", nrow(r_out), "x", ncol(r_out),
                    "| valores: [",
                    round(terra::global(r_out, "min", na.rm = TRUE)[[1]], 2),
                    ";",
                    round(terra::global(r_out, "max", na.rm = TRUE)[[1]], 2), "]"))
      generated_files <- c(generated_files, out_file)
    }, error = function(e) {
      log_msg(paste("[!] Erro ao salvar", var, ":", e$message))
      failed_vars <- c(failed_vars, var)
    })
  }
}

# ── Resumo ─────────────────────────────────────────────────────────────────────
log_msg(paste("[→] Resumo: gerados =", length(generated_files),
              "| pulados =", length(skipped_files),
              "| falhos =", length(failed_vars)))

# ── Salvar metrics.json ────────────────────────────────────────────────────────
metrics <- list(
  job_id          = job_id,
  engine          = "r",
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