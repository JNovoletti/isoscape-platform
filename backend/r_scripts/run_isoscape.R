#!/usr/bin/env Rscript
# =============================================================================
# run_isoscape.R
# Script standalone extraído do app.R — chamado pelo worker Celery via
# subprocess.run(["Rscript", "run_isoscape.R", ...])
#
# Uso:
#   Rscript run_isoscape.R \
#     --job-id        abc-123 \
#     --dataset-path  /data/datasets/amostras.csv \
#     --raster-dir    /data/rasters/project1/ \
#     --output-dir    /data/isoscapes/abc-123/ \
#     --response-col  d13C \
#     --lat-col       latitude \
#     --lon-col       longitude \
#     --uncertainty   quantile_rf \
#     --resolution    5 \
#     --seed          1350
# =============================================================================

suppressPackageStartupMessages({
  library(optparse)
  library(terra)
  library(sf)
  library(randomForest)
  library(ranger)
  library(VSURF)
  library(rsample)
  library(dplyr)
  library(readxl)
  library(jsonlite)
})

# ── CLI arguments ─────────────────────────────────────────────────────────────
option_list <- list(
  make_option("--job-id",       type = "character", help = "ID do Job (usado no log)"),
  make_option("--dataset-path", type = "character", help = "Caminho do CSV ou XLSX"),
  make_option("--raster-dir",   type = "character", help = "Diretório com os .tif recortados"),
  make_option("--output-dir",   type = "character", help = "Diretório de saída para os resultados"),
  make_option("--response-col", type = "character", help = "Nome da coluna resposta (ex: d13C)"),
  make_option("--lat-col",      type = "character", default = "latitude",    help = "Coluna de latitude"),
  make_option("--lon-col",      type = "character", default = "longitude",   help = "Coluna de longitude"),
  make_option("--uncertainty",  type = "character", default = "quantile_rf",
              help = "Método de incerteza: quantile_rf | bootstrap"),
  make_option("--resolution",   type = "character", default = "5",
              help = "Resolução dos rasters em arc-min: 2.5 | 5 | 10"),
  make_option("--seed",         type = "integer",   default = 1350L, help = "Semente de reprodutibilidade")
)

opt <- parse_args(OptionParser(option_list = option_list))

# ── Validação básica de argumentos ────────────────────────────────────────────
required <- c("job-id", "dataset-path", "raster-dir", "output-dir", "response-col")
missing  <- required[!required %in% names(opt) | sapply(required, function(x) is.null(opt[[x]]))]
if (length(missing) > 0) {
  stop("Argumentos obrigatórios ausentes: ", paste(missing, collapse = ", "))
}

job_id       <- opt[["job-id"]]
dataset_path <- opt[["dataset-path"]]
raster_dir   <- opt[["raster-dir"]]
output_dir   <- opt[["output-dir"]]
response_col <- opt[["response-col"]]
lat_col      <- opt[["lat-col"]]
lon_col      <- opt[["lon-col"]]
uncertainty  <- opt[["uncertainty"]]   # "quantile_rf" ou "bootstrap"
resolution   <- opt[["resolution"]]
seed         <- opt[["seed"]]

# ── Helpers de log ────────────────────────────────────────────────────────────
# Escreve em stdout (capturado pelo worker Python) e em log.txt no output_dir.
# Prefixos espelham o app.R original para facilitar a transição:
#   [✓]  etapa concluída
#   [→]  etapa iniciando
#   [!]  aviso / erro
#   [★]  concluído

log_path <- NULL  # definido após criar output_dir

log_msg <- function(msg) {
  line <- paste0("[", format(Sys.time(), "%H:%M:%S"), "] ", msg)
  cat(line, "\n", sep = "")
  if (!is.null(log_path)) {
    cat(line, "\n", sep = "", file = log_path, append = TRUE)
  }
}

# ── Criar diretório de saída ───────────────────────────────────────────────────
if (!dir.exists(output_dir)) dir.create(output_dir, recursive = TRUE)
log_path <- file.path(output_dir, "log.txt")

log_msg(paste("[→] Job iniciado:", job_id))
log_msg(paste("[→] Dataset:", dataset_path))
log_msg(paste("[→] Rasters:", raster_dir))
log_msg(paste("[→] Resolução:", resolution, "arc-min"))
log_msg(paste("[→] Incerteza:", uncertainty))

set.seed(seed)

# ── 1. Leitura dos dados ───────────────────────────────────────────────────────
log_msg("[→] Lendo dataset...")

ext <- tolower(tools::file_ext(dataset_path))
dados_raw <- tryCatch({
  if (ext == "csv") {
    read.csv(dataset_path)
  } else if (ext %in% c("xlsx", "xls")) {
    readxl::read_excel(dataset_path) %>% as.data.frame()
  } else {
    stop("Formato não suportado: ", ext)
  }
}, error = function(e) {
  log_msg(paste("[!] Erro ao ler dataset:", e$message))
  stop(e)
})

# Renomear colunas para nomes internos padronizados
required_cols <- c(lat_col, lon_col, response_col)
missing_cols  <- required_cols[!required_cols %in% names(dados_raw)]
if (length(missing_cols) > 0) {
  stop("Colunas ausentes no dataset: ", paste(missing_cols, collapse = ", "))
}

dados <- dados_raw %>%
  dplyr::select(all_of(c(lat_col, lon_col, response_col))) %>%
  dplyr::rename(
    latitude  = all_of(lat_col),
    longitude = all_of(lon_col),
    response  = all_of(response_col)
  )

for (col in c("latitude", "longitude", "response")) {
  if (!is.numeric(dados[[col]])) {
    stop("Coluna '", col, "' deve ser numérica.")
  }
}

log_msg(paste("[✓] Dataset lido:", nrow(dados), "linhas"))

# ── 2. Objeto espacial ────────────────────────────────────────────────────────
log_msg("[→] Criando objeto espacial...")
dados_sf <- st_as_sf(dados, coords = c("longitude", "latitude"), crs = 4674)
log_msg("[✓] Objeto sf criado (CRS SIRGAS 2000 / EPSG:4674)")

# ── 3. Stack de rasters ───────────────────────────────────────────────────────
log_msg("[→] Carregando rasters...")

# Aceita tanto os .tif passados diretamente (via --raster-paths futuro)
# quanto todos os .tif do diretório (comportamento atual)
raster_files <- list.files(raster_dir, pattern = "\\.tif$", full.names = TRUE)

if (length(raster_files) == 0) {
  stop("Nenhum .tif encontrado em: ", raster_dir)
}

r_stack <- rast(raster_files)
names(r_stack) <- tools::file_path_sans_ext(basename(raster_files))

# Reprojetar se necessário
if (!identical(st_crs(dados_sf)$wkt, crs(r_stack, proj = FALSE))) {
  r_stack <- project(r_stack, st_crs(dados_sf)$wkt)
}

log_msg(paste("[✓] Rasters carregados:", nlyr(r_stack), "camadas"))

# ── 4. Extração de valores ────────────────────────────────────────────────────
log_msg("[→] Extraindo valores dos rasters nos pontos amostrais...")

extracted <- terra::extract(r_stack, vect(dados_sf), method = "bilinear")
df_model  <- cbind(dados, extracted[, -1])  # remove coluna ID do extract
df_model  <- na.omit(df_model)

if (nrow(df_model) == 0) {
  stop("Nenhuma linha restou após remoção de NAs. Verifique se os pontos estão dentro da área dos rasters.")
}

resposta   <- df_model$response
preditoras <- df_model %>% dplyr::select(-latitude, -longitude, -response)

log_msg(paste("[✓] Extração concluída:", nrow(df_model), "linhas,", ncol(preditoras), "preditoras"))

# ── 5. Seleção de variáveis (VSURF) ───────────────────────────────────────────
log_msg("[→] Executando VSURF para seleção de variáveis...")
log_msg("    (esta etapa pode demorar vários minutos)")

vsurf_res <- tryCatch(
  VSURF::VSURF(as.matrix(preditoras), resposta, parallel = FALSE, verbose = FALSE),
  error = function(e) {
    log_msg(paste("[!] VSURF falhou:", e$message, "— usando todas as variáveis"))
    NULL
  }
)

if (!is.null(vsurf_res)) {
  threshold_vars <- if (length(vsurf_res$varselect.thres)  > 0) names(preditoras)[vsurf_res$varselect.thres]  else character(0)
  interp_vars    <- if (length(vsurf_res$varselect.interp) > 0) names(preditoras)[vsurf_res$varselect.interp] else character(0)
  pred_vars      <- if (length(vsurf_res$varselect.pred)   > 0) names(preditoras)[vsurf_res$varselect.pred]   else character(0)
} else {
  threshold_vars <- character(0)
  interp_vars    <- character(0)
  pred_vars      <- character(0)
}

if (length(pred_vars) == 0) pred_vars <- names(preditoras)

log_msg(paste("[✓] VSURF concluído. Variáveis preditoras:", paste(pred_vars, collapse = ", ")))

formula_rf <- as.formula(paste("response ~", paste(pred_vars, collapse = "+")))

# ── 6. Random Forest ─────────────────────────────────────────────────────────
log_msg("[→] Ajustando Random Forest (ntree = 500)...")

split  <- initial_split(df_model, prop = 0.8, strata = "response")
treino <- training(split)
teste  <- testing(split)

rf_mod    <- randomForest(formula_rf, data = treino, ntree = 500)
pred_test <- predict(rf_mod, teste)
MSE       <- mean((pred_test - teste$response)^2)
R2        <- cor(pred_test, teste$response)^2

log_msg(sprintf("[✓] RF ajustado. MSE = %.4f | R² = %.4f", MSE, R2))

# ── 7. Predição espacial ──────────────────────────────────────────────────────
log_msg("[→] Gerando isoscape (predição espacial)...")

isoscape_rast <- terra::predict(r_stack, rf_mod, na.rm = TRUE)

log_msg("[✓] Isoscape gerado")

# ── 8. Mapa de incerteza ──────────────────────────────────────────────────────
if (uncertainty == "quantile_rf") {
  log_msg("[→] Calculando incerteza via Quantile RF...")
  qrf_mod <- ranger(formula_rf, data = df_model, num.trees = 500, quantreg = TRUE)
  ci16    <- terra::predict(r_stack, qrf_mod, type = "quantiles", quantiles = 0.16)
  ci84    <- terra::predict(r_stack, qrf_mod, type = "quantiles", quantiles = 0.84)
  sd_map  <- (ci84 - ci16) / 2
} else {
  log_msg("[→] Calculando incerteza via Bootstrap (50 iterações)...")
  boot_preds <- replicate(50, {
    idx <- sample(seq_len(nrow(df_model)), replace = TRUE)
    m   <- randomForest(formula_rf, data = df_model[idx, ])
    terra::predict(r_stack, m)
  })
  sd_map <- app(boot_preds, sd)
}

log_msg("[✓] Mapa de incerteza gerado")

# ── 9. Salvar rasters de saída ────────────────────────────────────────────────
log_msg("[→] Salvando rasters...")

iso_path <- file.path(output_dir, "isoscape.tif")
sd_path  <- file.path(output_dir, "uncertainty.tif")

terra::writeRaster(isoscape_rast, iso_path, overwrite = TRUE)
terra::writeRaster(sd_map,        sd_path,  overwrite = TRUE)

log_msg(paste("[✓] isoscape.tif salvo em:", iso_path))
log_msg(paste("[✓] uncertainty.tif salvo em:", sd_path))

# ── 10. Salvar métricas em JSON ───────────────────────────────────────────────
# O worker Python lê este arquivo para popular o modelo Isoscape no banco.
metrics <- list(
  job_id         = job_id,
  MSE            = MSE,
  R2             = R2,
  threshold_vars = threshold_vars,
  interp_vars    = interp_vars,
  pred_vars      = pred_vars,
  isoscape_path  = iso_path,
  uncertainty_path = sd_path
)

metrics_path <- file.path(output_dir, "metrics.json")
write(jsonlite::toJSON(metrics, auto_unbox = TRUE, pretty = TRUE), metrics_path)

log_msg(paste("[✓] metrics.json salvo em:", metrics_path))
log_msg(paste("[★] Job concluído:", format(Sys.time(), "%Y-%m-%d %H:%M:%S")))