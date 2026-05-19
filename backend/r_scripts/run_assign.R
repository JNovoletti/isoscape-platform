#!/usr/bin/env Rscript
# =============================================================================
# run_assign.R
# Equivalente backend do 04_assign.R do curso.
# Atribuição de origem geográfica para amostras desconhecidas usando isoscape
# + Teorema de Bayes (via pacote assignR).
#
# Saídas (sufixo "_r"):
#   pd_map_<sample_id>_r.tif         -- mapa de densidade posterior (pdRaster)
#   qtl_area_<sample_id>_r.tif       -- threshold por área (default 0.5)
#   qtl_prob_<sample_id>_r.tif       -- threshold por prob acumulada (0.95)
#   odds_ratios_r.csv                -- razão de chances entre regiões
#   posterior_probs_r.csv            -- probabilidade posterior por região
#   metrics.json                     -- resumo + caminhos
#
# Uso:
#   Rscript run_assign.R \
#     --job-id          abc-123 \
#     --isoscape-path   /data/isoscapes/X/isoscape_combined_r.tif \
#     --unknown-path    /data/datasets/unknowns.csv \
#     --regions-shp     /data/shapefiles/fu.shp \
#     --regions-field   ADM1_PT \
#     --regions-filter  "Amazonas,Mato Grosso" \
#     --output-dir      /data/assignments/abc-123/ \
#     --response-col    d13C_wood \
#     --area-threshold  0.5 \
#     --prob-threshold  0.95 \
#     --seed            1350
# =============================================================================

suppressPackageStartupMessages({
  library(optparse)
  library(terra)
  library(sf)
  library(assignR)
  library(dplyr)
  library(readxl)
  library(jsonlite)
})

# ── CLI ───────────────────────────────────────────────────────────────────────
option_list <- list(
  make_option("--job-id",          type = "character", help = "ID do Job"),
  make_option("--isoscape-path",   type = "character",
              help = "Caminho do isoscape_combined.tif (2 bandas: predição + sd)"),
  make_option("--unknown-path",    type = "character",
              help = "CSV/XLSX com as amostras desconhecidas (precisa de ID + response-col)"),
  make_option("--regions-shp",     type = "character",
              help = "Shapefile das regiões candidatas (ex: UFs)"),
  make_option("--regions-field",   type = "character", default = "ADM1_PT",
              help = "Campo do shapefile para filtrar/identificar regiões"),
  make_option("--regions-filter",  type = "character", default = "",
              help = "Lista de regiões separadas por vírgula (vazio = todas)"),
  make_option("--output-dir",      type = "character", help = "Diretório de saída"),
  make_option("--response-col",    type = "character", default = "d13C_wood",
              help = "Nome da coluna isotópica nas amostras desconhecidas"),
  make_option("--area-threshold",  type = "numeric",   default = 0.5,
              help = "Threshold por área para qtlRaster (default 0.5)"),
  make_option("--prob-threshold",  type = "numeric",   default = 0.95,
              help = "Threshold por probabilidade acumulada para qtlRaster (default 0.95)"),
  make_option("--seed",            type = "integer",   default = 1350L)
)

opt <- parse_args(OptionParser(option_list = option_list))

required <- c("job-id", "isoscape-path", "unknown-path", "regions-shp", "output-dir")
missing  <- required[sapply(required, function(x) is.null(opt[[x]]))]
if (length(missing) > 0) stop("Argumentos obrigatórios ausentes: ", paste(missing, collapse = ", "))

job_id          <- opt[["job-id"]]
isoscape_path   <- opt[["isoscape-path"]]
unknown_path    <- opt[["unknown-path"]]
regions_shp     <- opt[["regions-shp"]]
regions_field   <- opt[["regions-field"]]
regions_filter  <- opt[["regions-filter"]]
output_dir      <- opt[["output-dir"]]
response_col    <- opt[["response-col"]]
area_threshold  <- opt[["area-threshold"]]
prob_threshold  <- opt[["prob-threshold"]]
seed            <- opt[["seed"]]

if (!dir.exists(output_dir)) dir.create(output_dir, recursive = TRUE)
log_path <- file.path(output_dir, "log.txt")

log_msg <- function(msg) {
  line <- paste0("[", format(Sys.time(), "%H:%M:%S"), "] ", msg)
  cat(line, "\n", sep = "")
  cat(line, "\n", sep = "", file = log_path, append = TRUE)
}

set.seed(seed)
RNGkind(kind = "Mersenne-Twister", normal.kind = "Inversion", sample.kind = "Rounding")

log_msg(paste("[→] Job run_assign iniciado:", job_id))
log_msg(paste("[→] Isoscape:", isoscape_path))
log_msg(paste("[→] Unknowns:", unknown_path))
log_msg(paste("[→] Shapefile de regiões:", regions_shp))
log_msg("[→] Engine: R")

# ── 1. Carregar isoscape + sd ─────────────────────────────────────────────────
log_msg("[→] Carregando isoscape...")
iso_d13 <- terra::rast(isoscape_path)
n_bands <- terra::nlyr(iso_d13)
log_msg(paste("[✓] Isoscape:", n_bands, "bandas | CRS:", crs(iso_d13, describe = TRUE)$code))

if (n_bands < 2) {
  log_msg("[!] Isoscape com menos de 2 bandas — assignR precisa de [predição, sd]")
  log_msg("[!] Tentando assumir banda única + sd=0 (não recomendado)")
}

# ── 2. Carregar regiões (UFs ou similar) ──────────────────────────────────────
log_msg("[→] Carregando shapefile de regiões...")
fu <- terra::vect(regions_shp)
fu <- terra::project(fu, "EPSG:4674")
log_msg(paste("[✓] Regiões carregadas:", nrow(fu), "features"))
log_msg(paste("[→] Campos disponíveis:", paste(names(fu), collapse = ", ")))

if (!regions_field %in% names(fu)) {
  stop("Campo '", regions_field, "' não encontrado no shapefile. Disponíveis: ",
       paste(names(fu), collapse = ", "))
}

# Filtrar regiões
if (nchar(regions_filter) > 0) {
  fu_names <- trimws(strsplit(regions_filter, ",")[[1]])
  fus <- fu[fu[[regions_field]][[1]] %in% fu_names]
  log_msg(paste("[→] Regiões filtradas:", paste(fu_names, collapse = ", ")))
} else {
  fus <- fu
  fu_names <- unique(as.character(fu[[regions_field]][[1]]))
  log_msg(paste("[→] Usando todas as", length(fu_names), "regiões"))
}

if (nrow(fus) == 0) stop("Nenhuma região após o filtro.")

# Garantir alinhamento de CRS
if (!terra::same.crs(fus, iso_d13)) {
  log_msg("[→] Reprojetando regiões para o CRS do isoscape...")
  fus <- terra::project(fus, iso_d13)
}

# ── 3. Média do isoscape por região ───────────────────────────────────────────
log_msg("[→] Calculando média do isoscape por região...")
fus_mu <- terra::extract(iso_d13[[1]], fus, fun = mean, na.rm = TRUE)
# Substituir ID numérico pelo nome da região
fus_mu$ID <- as.character(fus[[regions_field]][[1]])
log_msg("[✓] Médias por região:")
for (i in seq_len(nrow(fus_mu))) {
  log_msg(sprintf("    %s: %.4f", fus_mu$ID[i], fus_mu[i, 2]))
}

# ── 4. Carregar amostras desconhecidas ────────────────────────────────────────
log_msg("[→] Carregando amostras desconhecidas...")
ext <- tolower(tools::file_ext(unknown_path))
unknowns_raw <- if (ext == "csv") {
  read.csv(unknown_path)
} else if (ext %in% c("xlsx", "xls")) {
  as.data.frame(readxl::read_excel(unknown_path))
} else {
  stop("Formato não suportado: ", ext)
}

# Validar colunas: precisa de ID, longitude, latitude (ou x, y) e response_col
# Espelha pdRaster() do assignR, que espera (ID, response_value)
# Aceita 'x'/'y' OU 'longitude'/'latitude'
has_xy   <- all(c("x", "y") %in% names(unknowns_raw))
has_ll   <- all(c("longitude", "latitude") %in% names(unknowns_raw))
if (!has_xy && !has_ll) {
  stop("Amostras desconhecidas precisam de colunas (x,y) ou (longitude,latitude)")
}
if (!response_col %in% names(unknowns_raw)) {
  stop("Coluna '", response_col, "' não encontrada nas amostras")
}
if (!"ID" %in% names(unknowns_raw)) {
  unknowns_raw$ID <- seq_len(nrow(unknowns_raw))
  log_msg("[→] Coluna ID não fornecida — criando IDs sequenciais")
}

# Padronizar coordenadas
if (!has_xy && has_ll) {
  unknowns_raw$x <- unknowns_raw$longitude
  unknowns_raw$y <- unknowns_raw$latitude
}

unknowns <- unknowns_raw[!is.na(unknowns_raw[[response_col]]), ]
log_msg(paste("[✓]", nrow(unknowns), "amostras desconhecidas com valor isotópico"))

# ── 5. pdRaster (mapa de probabilidade posterior por amostra) ────────────────
log_msg("[→] Gerando pdRaster para cada amostra desconhecida...")

# pdRaster espera data.frame com (ID, response) — UMA AMOSTRA POR VEZ no nosso fluxo
# (para evitar criar arquivos enormes quando há centenas de unknowns)
pd_paths   <- character(0)
qtla_paths <- character(0)
qtlp_paths <- character(0)
sample_summary <- list()

for (i in seq_len(nrow(unknowns))) {
  unk <- unknowns[i, ]
  sample_id <- as.character(unk$ID)

  log_msg(sprintf("[→] Amostra %s (%s = %.3f)", sample_id, response_col, unk[[response_col]]))

  sam <- data.frame(ID = sample_id, d13C = unk[[response_col]])

  pd <- tryCatch(
    assignR::pdRaster(iso_d13, sam, genplot = FALSE),
    error = function(e) {
      log_msg(paste("  [!] pdRaster falhou para", sample_id, ":", e$message))
      NULL
    }
  )

  if (is.null(pd)) next

  pd_path <- file.path(output_dir, paste0("pd_map_", sample_id, "_r.tif"))
  terra::writeRaster(pd, pd_path, overwrite = TRUE)
  pd_paths <- c(pd_paths, pd_path)
  log_msg(paste("  [✓] pd_map salvo:", basename(pd_path)))

  # qtlRaster por área
  qtla <- tryCatch(
    assignR::qtlRaster(pd, threshold = area_threshold, genplot = FALSE),
    error = function(e) {
      log_msg(paste("  [!] qtlRaster (area) falhou:", e$message))
      NULL
    }
  )
  if (!is.null(qtla)) {
    qtla_path <- file.path(output_dir, paste0("qtl_area_", sample_id, "_r.tif"))
    terra::writeRaster(qtla, qtla_path, overwrite = TRUE)
    qtla_paths <- c(qtla_paths, qtla_path)
  }

  # qtlRaster por probabilidade
  qtlp <- tryCatch(
    assignR::qtlRaster(pd, threshold = prob_threshold,
                       thresholdType = "prob", genplot = FALSE),
    error = function(e) {
      log_msg(paste("  [!] qtlRaster (prob) falhou:", e$message))
      NULL
    }
  )
  if (!is.null(qtlp)) {
    qtlp_path <- file.path(output_dir, paste0("qtl_prob_", sample_id, "_r.tif"))
    terra::writeRaster(qtlp, qtlp_path, overwrite = TRUE)
    qtlp_paths <- c(qtlp_paths, qtlp_path)
  }

  # Razão de chances entre regiões (oddsRatio)
  odds <- tryCatch(
    as.data.frame(assignR::oddsRatio(pd, fus)),
    error = function(e) {
      log_msg(paste("  [!] oddsRatio falhou:", e$message))
      NULL
    }
  )

  # Posterior por região (extrair PD em cada polígono e normalizar)
  d1 <- terra::extract(iso_d13[[1]], fus)
  posterior <- sapply(seq_len(nrow(fus)), function(k) {
    vals <- d1[d1$ID == k, 2]
    vals <- vals[!is.na(vals)]
    if (length(vals) == 0) return(NA_real_)
    dens <- density(vals)
    dens$y[which.min(abs(unk[[response_col]] - dens$x))]
  })
  posterior_norm <- if (sum(posterior, na.rm = TRUE) > 0) {
    posterior / sum(posterior, na.rm = TRUE)
  } else {
    rep(NA_real_, length(posterior))
  }

  sample_summary[[sample_id]] <- list(
    sample_id    = sample_id,
    iso_value    = unk[[response_col]],
    pd_path      = pd_path,
    odds_ratios  = odds,
    posterior    = setNames(as.list(posterior_norm), as.character(fus[[regions_field]][[1]])),
    most_likely  = as.character(fus[[regions_field]][[1]])[which.max(posterior_norm)]
  )

  log_msg(sprintf("  [✓] Região mais provável: %s",
                  sample_summary[[sample_id]]$most_likely))
}

# ── 6. Tabelas consolidadas ───────────────────────────────────────────────────
posterior_rows <- do.call(rbind, lapply(sample_summary, function(s) {
  data.frame(
    sample_id   = s$sample_id,
    iso_value   = s$iso_value,
    region      = names(s$posterior),
    posterior   = unlist(s$posterior),
    stringsAsFactors = FALSE,
    row.names = NULL
  )
}))
posterior_csv <- file.path(output_dir, "posterior_probs_r.csv")
write.csv(posterior_rows, posterior_csv, row.names = FALSE)
log_msg(paste("[✓] Tabela de probabilidades posteriores salva:", basename(posterior_csv)))

odds_rows <- do.call(rbind, lapply(sample_summary, function(s) {
  if (is.null(s$odds_ratios)) return(NULL)
  df <- s$odds_ratios
  df$sample_id <- s$sample_id
  df
}))
odds_csv <- file.path(output_dir, "odds_ratios_r.csv")
if (!is.null(odds_rows)) {
  write.csv(odds_rows, odds_csv, row.names = FALSE)
  log_msg(paste("[✓] Tabela de odds ratios salva:", basename(odds_csv)))
}

# ── 7. metrics.json ───────────────────────────────────────────────────────────
metrics <- list(
  job_id            = job_id,
  engine            = "r",
  n_unknowns        = nrow(unknowns),
  regions           = as.character(fus[[regions_field]][[1]]),
  region_means      = setNames(as.list(fus_mu[, 2]), fus_mu$ID),
  pd_maps           = pd_paths,
  qtl_area_maps     = qtla_paths,
  qtl_prob_maps     = qtlp_paths,
  posterior_csv     = posterior_csv,
  odds_csv          = if (!is.null(odds_rows)) odds_csv else NULL,
  area_threshold    = area_threshold,
  prob_threshold    = prob_threshold
)

metrics_path <- file.path(output_dir, "metrics.json")
write(jsonlite::toJSON(metrics, auto_unbox = TRUE, pretty = TRUE, na = "null"),
      metrics_path)

log_msg(paste("[✓] metrics.json salvo:", metrics_path))
log_msg(paste("[★] run_assign concluído:", format(Sys.time(), "%Y-%m-%d %H:%M:%S")))