#!/usr/bin/env Rscript
# =============================================================================
# run_isoscape.R
# Equivalente backend dos scripts 02_extr_dados_raster.R + 03_integracao_ML.R
# do curso. Treina Random Forest sobre amostras + rasters preditores e gera
# isoscape + mapa de incerteza.
#
# Paridade fiel com 03_integracao_ML.R:
#   - set_reproducibility(): set.seed + RNGkind(Mersenne-Twister, Inversion, Rounding)
#   - VSURF com ntree=500, nfor.thres=20, nfor.interp=100, nfor.pred=10, nsd=1
#   - randomForest com ntree=2000, importance=TRUE, keep.forest=TRUE
#   - caret::createDataPartition reimplementado em base R (sem dep)
#   - ranger quantreg para mapa de incerteza (Q0.84 - Q0.16) / 2
#
# Sufixo "_r" nos outputs diferencia da versão Python ("_py"):
#   isoscape_r.tif | uncertainty_r.tif | dataset_with_vars_r.csv
#
# Uso:
#   Rscript run_isoscape.R \
#     --job-id        abc-123 \
#     --dataset-path  /data/datasets/madeiras.csv \
#     --raster-dir    /data/rasters/project1/ \
#     --output-dir    /data/isoscapes/abc-123/ \
#     --response-col  d13C_wood \
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
  library(dplyr)
  library(readxl)
  library(jsonlite)
})

# ── Replacement de caret::createDataPartition (evita dependência pesada) ──────
# Reproduz fielmente o algoritmo do caret para y numérico:
#   1. Bina y em min(5, length(y)) quantis com cut() + quantile()
#   2. Dentro de cada bin, amostra ceiling(n_bin * p) índices aleatórios
#   3. Retorna os índices de treino ordenados
# Referência: github.com/topepo/caret/blob/master/pkg/caret/R/createDataPartition.R
create_data_partition <- function(y, p = 0.5, groups = min(5, length(y))) {
  if (length(y) < 2) stop("y deve ter pelo menos 2 pontos")
  if (groups < 2)   groups <- 2

  if (is.numeric(y)) {
    breaks <- unique(quantile(y, probs = seq(0, 1, length.out = groups)))
    y_bin <- cut(y, breaks, include.lowest = TRUE)
  } else {
    y_bin <- factor(as.character(y))
  }

  idx_all <- seq_along(y)
  train_idx <- unlist(lapply(split(idx_all, y_bin), function(bin) {
    if (length(bin) == 0) return(integer(0))
    if (length(bin) == 1) return(bin)
    sample(bin, size = ceiling(length(bin) * p))
  }))
  sort(as.integer(train_idx))
}

# ── CLI arguments ─────────────────────────────────────────────────────────────
option_list <- list(
  make_option("--job-id",       type = "character", help = "ID do Job (usado no log)"),
  make_option("--dataset-path", type = "character", help = "Caminho do CSV ou XLSX"),
  make_option("--raster-dir",   type = "character", help = "Diretório com os .tif recortados"),
  make_option("--output-dir",   type = "character", help = "Diretório de saída para os resultados"),
  make_option("--response-col", type = "character", help = "Nome da coluna resposta (ex: d13C_wood)"),
  make_option("--lat-col",      type = "character", default = "latitude",    help = "Coluna de latitude"),
  make_option("--lon-col",      type = "character", default = "longitude",   help = "Coluna de longitude"),
  make_option("--uncertainty",  type = "character", default = "quantile_rf",
              help = "Método de incerteza: quantile_rf | bootstrap"),
  make_option("--resolution",   type = "character", default = "5",
              help = "Resolução dos rasters em arc-min: 2.5 | 5 | 10"),
  make_option("--seed",         type = "integer",   default = 1350L, help = "Semente de reprodutibilidade"),
  make_option("--raster-suffix", type = "character", default = "_r",
              help = "Filtro de sufixo no nome dos rasters (default: _r). Use '' para todos.")
)

opt <- parse_args(OptionParser(option_list = option_list))

# ── Validação básica de argumentos ────────────────────────────────────────────
required <- c("job-id", "dataset-path", "raster-dir", "output-dir", "response-col")
missing  <- required[!required %in% names(opt) | sapply(required, function(x) is.null(opt[[x]]))]
if (length(missing) > 0) {
  stop("Argumentos obrigatórios ausentes: ", paste(missing, collapse = ", "))
}

job_id        <- opt[["job-id"]]
dataset_path  <- opt[["dataset-path"]]
raster_dir    <- opt[["raster-dir"]]
output_dir    <- opt[["output-dir"]]
response_col  <- opt[["response-col"]]
lat_col       <- opt[["lat-col"]]
lon_col       <- opt[["lon-col"]]
uncertainty   <- opt[["uncertainty"]]
resolution    <- opt[["resolution"]]
seed          <- opt[["seed"]]
raster_suffix <- opt[["raster-suffix"]]

# ── Helper de reprodutibilidade — copiado do 03_integracao_ML.R ────────────────
set_reproducibility <- function(seed_val = seed) {
  set.seed(seed_val)
  RNGkind(kind = "Mersenne-Twister", normal.kind = "Inversion", sample.kind = "Rounding")
}

# ── Helpers de log ────────────────────────────────────────────────────────────
log_path <- NULL

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
log_msg(paste("[→] Sufixo de filtro:", if (nchar(raster_suffix) > 0) raster_suffix else "(nenhum — todos)"))
log_msg(paste("[→] Resolução:", resolution, "arc-min"))
log_msg(paste("[→] Incerteza:", uncertainty))
log_msg(paste("[→] Seed:", seed))
log_msg("[→] Engine: R")

set_reproducibility()

# ── 1. Leitura dos dados (espelho do 02_extr_dados_raster.R) ──────────────────
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

# Validar colunas obrigatórias
required_cols <- c(lat_col, lon_col, response_col)
missing_cols  <- required_cols[!required_cols %in% names(dados_raw)]
if (length(missing_cols) > 0) {
  stop("Colunas ausentes no dataset: ", paste(missing_cols, collapse = ", "))
}

# Selecionar apenas as colunas necessárias e padronizar nomes internos
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

# Tratando NAs na resposta (espelho do 03_integracao_ML.R)
dados <- dados[!is.na(dados$response), ]
log_msg(paste("[✓] Dataset lido:", nrow(dados), "linhas válidas (NA na resposta removidos)"))

# ── 2. Objeto espacial sf (espelho do 02_extr_dados_raster.R) ─────────────────
log_msg("[→] Criando objeto espacial (CRS SIRGAS 2000 / EPSG:4674)...")
dados_sf <- st_as_sf(dados, coords = c("longitude", "latitude"), crs = 4674)
log_msg("[✓] Objeto sf criado")

# ── 3. Stack de rasters ───────────────────────────────────────────────────────
log_msg("[→] Carregando rasters...")

# Filtrar por sufixo: pega apenas {raster_dir}/*{suffix}.tif quando suffix != ""
pattern <- if (nchar(raster_suffix) > 0) {
  paste0(raster_suffix, "\\.tif$")
} else {
  "\\.tif$"
}
raster_files <- list.files(raster_dir, pattern = pattern, full.names = TRUE)
log_msg(paste("[→] Arquivos .tif encontrados:", length(raster_files)))

# Fallback: se não achou nada com o sufixo, tenta todos os .tif e avisa
if (length(raster_files) == 0 && nchar(raster_suffix) > 0) {
  log_msg(paste("[!] Nenhum raster com sufixo", raster_suffix,
                "— buscando todos os .tif"))
  raster_files <- list.files(raster_dir, pattern = "\\.tif$", full.names = TRUE)
  log_msg(paste("[→] Arquivos .tif (fallback):", length(raster_files)))
}

if (length(raster_files) == 0) {
  stop("Nenhum .tif encontrado em: ", raster_dir)
}

log_msg(paste("[→] Primeiro raster:", basename(raster_files[[1]])))

# Espelha o pattern do 02_extr_dados_raster.R: lapply + rast
raster_list <- lapply(raster_files, terra::rast)
r_stack <- tryCatch({
  rast(raster_list)
}, error = function(e) {
  log_msg(paste("[!] Erro ao empilhar rasters:", e$message))
  stop(e)
})

# Nomear camadas SEM o sufixo de engine para que o nome bata com Python
# Ex: "amazonia_legal_5arc_tavg_mean_r" → "amazonia_legal_5arc_tavg_mean"
names(r_stack) <- gsub(paste0(raster_suffix, "$"), "",
                       tools::file_path_sans_ext(basename(raster_files)))

# Reprojetar se necessário
if (!identical(st_crs(dados_sf)$wkt, crs(r_stack, proj = FALSE))) {
  r_stack <- terra::project(r_stack, st_crs(dados_sf)$wkt)
}

log_msg(paste("[✓] Rasters carregados:", nlyr(r_stack), "camadas:",
              paste(names(r_stack), collapse = ", ")))

# ── 4. Extração de valores (espelho do 02_extr_dados_raster.R) ────────────────
log_msg("[→] Extraindo valores dos rasters nos pontos amostrais (método bilinear)...")

extracted <- terra::extract(r_stack, terra::vect(dados_sf), method = "bilinear")
df_model  <- cbind(dados, extracted[, -1, drop = FALSE])
df_model  <- na.omit(df_model)

if (nrow(df_model) == 0) {
  stop("Nenhuma linha restou após remoção de NAs. Verifique se os pontos estão dentro da área dos rasters.")
}

# Salvar o dataset combinado (equivalente ao madeira_amz_var_clim.xlsx)
combined_csv <- file.path(output_dir, "dataset_with_vars_r.csv")
write.csv(df_model, combined_csv, row.names = FALSE)
log_msg(paste("[✓] Dataset combinado salvo:", basename(combined_csv)))

resposta   <- df_model$response
preditoras <- df_model %>% dplyr::select(-latitude, -longitude, -response)

log_msg(paste("[✓] Extração concluída:", nrow(df_model), "linhas,",
              ncol(preditoras), "preditoras"))

# Sanity check de NAs nas preditoras (espelho do 03_integracao_ML.R)
if (anyNA(preditoras)) {
  log_msg("[!] Aviso: NAs encontrados nas preditoras após extração — serão tratados pelo na.omit")
} else {
  log_msg("[✓] Sem NAs nas preditoras")
}

# ── 5. Seleção de variáveis (VSURF) — parâmetros do 03_integracao_ML.R ────────
# Fast-path: VSURF não faz sentido (e pode quebrar com bug 'min.pred not found')
# quando há menos de 2 preditoras — nesse caso, pula direto.
if (ncol(preditoras) < 2) {
  log_msg(paste("[!] Apenas", ncol(preditoras),
                "preditora — pulando VSURF e usando-a diretamente"))
  threshold_vars <- names(preditoras)
  interp_vars    <- names(preditoras)
  pred_vars      <- names(preditoras)
} else {
  log_msg("[→] Executando VSURF para seleção de variáveis...")
  log_msg("    (ntree=500, nfor.thres=20, nfor.interp=100, nfor.pred=10, nsd=1)")
  log_msg("    (esta etapa pode demorar vários minutos)")

  set_reproducibility()

  preditoras_matrix <- as.matrix(preditoras)

  vsurf_res <- tryCatch(
    VSURF::VSURF(preditoras_matrix, resposta,
                 ntree       = 500,
                 nfor.thres  = 20,
                 nfor.interp = 100,
                 nfor.pred   = 10,
                 nsd         = 1,
                 parallel    = FALSE,
                 verbose     = FALSE),
    error = function(e) {
      log_msg(paste("[!] VSURF falhou:", e$message))
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

  if (length(pred_vars) == 0) {
    log_msg("[!] VSURF não selecionou variáveis — usando todas como fallback")
    pred_vars <- names(preditoras)
  }
}

log_msg(paste("[✓] VSURF — Threshold:", paste(threshold_vars, collapse = ", ")))
log_msg(paste("[✓] VSURF — Interp:   ", paste(interp_vars,    collapse = ", ")))
log_msg(paste("[✓] VSURF — Pred:     ", paste(pred_vars,      collapse = ", ")))

formula_rf <- as.formula(paste("response ~", paste(pred_vars, collapse = "+")))
log_msg(paste("[→] Fórmula:", deparse(formula_rf)))

# ── 6. Random Forest "full" (espelho do 03_integracao_ML.R) ───────────────────
log_msg("[→] Ajustando Random Forest principal (ntree = 2000, importance = TRUE)...")

set_reproducibility()
rf_mod1 <- randomForest::randomForest(
  formula_rf,
  data        = df_model,
  ntree       = 2000,
  importance  = TRUE,
  keep.forest = TRUE
)

log_msg(paste("[✓] RF principal ajustado | % Var Explicada:",
              round(tail(rf_mod1$rsq, 1) * 100, 2), "%"))

# ── 7. Split treino/teste — estratificado por quantis da resposta ─────────────
# Equivalente a caret::createDataPartition(p = 0.8, list = FALSE), mas sem
# a dependência pesada do caret. Algoritmo idêntico: cut() + quantile() +
# amostra ceiling(n_bin * p) por bin.
log_msg("[→] Split treino/teste estratificado por quantis (p = 0.8)...")

set_reproducibility()
partition <- create_data_partition(df_model$response, p = 0.8)
treino <- df_model[partition, ]
teste  <- df_model[-partition, ]

log_msg(paste("[→] Treino:", nrow(treino), "linhas | Teste:", nrow(teste), "linhas"))

set_reproducibility()
rf_train  <- randomForest::randomForest(formula_rf, data = treino, ntree = 500)
pred_test <- predict(rf_train, teste)

MSE <- mean((pred_test - teste$response)^2)
R2  <- cor(pred_test, teste$response)^2

log_msg(sprintf("[✓] Avaliação no teste — MSE = %.4f | R² = %.4f", MSE, R2))

# ── 8. Predição espacial — isoscape ───────────────────────────────────────────
log_msg("[→] Gerando isoscape (predição espacial sobre o stack)...")

# Garantir que o stack tenha apenas as preditoras usadas pelo modelo (ordem e nomes)
r_stack_pred <- r_stack[[pred_vars]]
isoscape_rast <- terra::predict(r_stack_pred, rf_mod1, na.rm = TRUE)

log_msg("[✓] Isoscape gerado")

# ── 9. Mapa de incerteza ──────────────────────────────────────────────────────
if (uncertainty == "quantile_rf") {
  log_msg("[→] Calculando incerteza via Quantile RF (ranger, num.trees = 500)...")
  set_reproducibility()
  qrf_mod <- ranger::ranger(formula_rf, data = df_model,
                            num.trees = 500, quantreg = TRUE)
  ci16    <- terra::predict(r_stack_pred, qrf_mod,
                            type = "quantiles", quantiles = 0.16, na.rm = TRUE)
  ci84    <- terra::predict(r_stack_pred, qrf_mod,
                            type = "quantiles", quantiles = 0.84, na.rm = TRUE)
  sd_map  <- (ci84 - ci16) / 2
} else {
  log_msg("[→] Calculando incerteza via Bootstrap (50 iterações)...")
  set_reproducibility()
  boot_preds <- terra::rast(replicate(50, {
    idx <- sample(seq_len(nrow(df_model)), replace = TRUE)
    m   <- randomForest::randomForest(formula_rf, data = df_model[idx, ], ntree = 500)
    terra::predict(r_stack_pred, m, na.rm = TRUE)
  }))
  sd_map <- terra::app(boot_preds, sd)
}

log_msg("[✓] Mapa de incerteza gerado")

# ── 10. Salvar rasters de saída (sufixo _r) ───────────────────────────────────
log_msg("[→] Salvando rasters...")

iso_path <- file.path(output_dir, "isoscape_r.tif")
sd_path  <- file.path(output_dir, "uncertainty_r.tif")

# Concatena predição + sd em um único multi-band (mesmo padrão do 03_integracao_ML.R)
isoscape_d13c <- c(isoscape_rast, sd_map)
names(isoscape_d13c) <- c("isoscape", "sd")

terra::writeRaster(isoscape_rast, iso_path, overwrite = TRUE)
terra::writeRaster(sd_map,        sd_path,  overwrite = TRUE)

# Também salva versão combinada (igual ao isoscape_dc13.tif do curso)
combined_path <- file.path(output_dir, "isoscape_combined_r.tif")
terra::writeRaster(isoscape_d13c, combined_path, overwrite = TRUE)

log_msg(paste("[✓] isoscape_r.tif salvo em:", iso_path))
log_msg(paste("[✓] uncertainty_r.tif salvo em:", sd_path))
log_msg(paste("[✓] isoscape_combined_r.tif (2 bandas) salvo em:", combined_path))

# ── 11. Importância das variáveis (espelho do varImp do 03) ───────────────────
imp_df <- as.data.frame(randomForest::importance(rf_mod1))
imp_df$variable <- rownames(imp_df)
rownames(imp_df) <- NULL
imp_path <- file.path(output_dir, "variable_importance_r.csv")
write.csv(imp_df, imp_path, row.names = FALSE)
log_msg(paste("[✓] variable_importance_r.csv salvo"))

# ── 12. Salvar métricas em JSON ───────────────────────────────────────────────
# O worker Python lê este arquivo para popular o modelo Isoscape no banco.
metrics <- list(
  job_id            = job_id,
  engine            = "r",
  MSE               = MSE,
  R2                = R2,
  threshold_vars    = threshold_vars,
  interp_vars       = interp_vars,
  pred_vars         = pred_vars,
  isoscape_path     = iso_path,
  uncertainty_path  = sd_path,
  combined_path     = combined_path,
  importance_path   = imp_path,
  dataset_extracted = combined_csv,
  n_samples         = nrow(df_model),
  n_train           = nrow(treino),
  n_test            = nrow(teste)
)

metrics_path <- file.path(output_dir, "metrics.json")
write(jsonlite::toJSON(metrics, auto_unbox = TRUE, pretty = TRUE), metrics_path)

log_msg(paste("[✓] metrics.json salvo em:", metrics_path))
log_msg(paste("[★] Job concluído:", format(Sys.time(), "%Y-%m-%d %H:%M:%S")))