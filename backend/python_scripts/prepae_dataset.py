#!/usr/bin/env python
"""
backend/python_scripts/prepare_dataset.py

Pré-processamento de dataset de amostras para o pipeline de isoscape.

Problema: datasets como madeiras.csv têm múltiplas amostras (ratio_point) por
árvore (mesma lat/lon). Tratar cada linha como ponto independente vaza
informação de validação e infla artificialmente o tamanho amostral.

Este script colapsa essas duplicatas:
  1. Agrupa por (latitude, longitude) arredondados a `coord_precision` casas
  2. Agrega `value_cols` com o método escolhido (mean, median, ...)
  3. Mantém `keep_cols` usando a primeira ocorrência (ex: Site, Family)
  4. Para cada coluna agregada, adiciona `<col>_sd` e `<col>_n`
  5. Grava CSV agregado e metrics.json com estatísticas da operação

Uso:
    python prepare_dataset.py \\
      --job-id abc-123 \\
      --input  /data/datasets/madeiras.csv \\
      --output /data/datasets/madeiras_aggregated.csv \\
      --output-dir /data/prepared/abc-123/ \\
      --lat-col latitude \\
      --lon-col longitude \\
      --value-cols d13C_wood \\
      --keep-cols Site,Family \\
      --agg-method mean \\
      --coord-precision 6
"""

import argparse
import sys
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from python_scripts.utils import (
    setup_logging, log_msg, MetricsCollector, ensure_output_dir, parse_csv_list,
)


AGG_METHODS = ("mean", "median", "min", "max", "sum")


def aggregate_duplicates(
    df: pd.DataFrame,
    lat_col: str,
    lon_col: str,
    value_cols: List[str],
    keep_cols: List[str],
    agg_method: str = "mean",
    coord_precision: int = 6,
    logger=None,
) -> tuple[pd.DataFrame, dict]:
    """
    Colapsa duplicatas por (lat, lon) arredondados a `coord_precision` casas.

    Retorna (df_agregado, stats).

    stats contém:
      input_rows, output_rows, duplicates_collapsed, max_replicates_per_point,
      mean_replicates_per_point, points_with_replicates
    """
    if agg_method not in AGG_METHODS:
        raise ValueError(f"agg_method inválido: {agg_method!r}. Use um de {AGG_METHODS}")

    for c in [lat_col, lon_col] + value_cols:
        if c not in df.columns:
            raise ValueError(f"Coluna ausente no dataset: {c!r}")

    # Validar keep_cols (silenciosamente ignorar as ausentes, mas avisar)
    keep_cols_present = [c for c in keep_cols if c in df.columns]
    keep_cols_missing = [c for c in keep_cols if c not in df.columns]
    if keep_cols_missing and logger is not None:
        log_msg(logger, f"[!] Colunas a preservar não encontradas: {keep_cols_missing}", "WARNING")

    # Criar chaves arredondadas (sem mexer nas colunas originais)
    df = df.copy()
    df["__lat_key"] = df[lat_col].round(coord_precision)
    df["__lon_key"] = df[lon_col].round(coord_precision)

    # Converter colunas de valor pra numérico (warn se algum NaN aparecer)
    for col in value_cols:
        before_na = df[col].isna().sum()
        df[col]   = pd.to_numeric(df[col], errors="coerce")
        new_na    = df[col].isna().sum() - before_na
        if new_na > 0 and logger is not None:
            log_msg(logger, f"[!] {col}: {new_na} valores não-numéricos viraram NaN", "WARNING")

    # Descartar linhas sem coordenadas válidas
    n_before = len(df)
    df = df.dropna(subset=["__lat_key", "__lon_key"])
    n_dropped_coords = n_before - len(df)
    if n_dropped_coords > 0 and logger is not None:
        log_msg(logger, f"[!] {n_dropped_coords} linhas descartadas por falta de coordenada", "WARNING")

    # Construir agregação:
    #   - para cada value_col: agg_method, sd, n (não-NA)
    #   - para cada keep_col:  first
    #   - lat/lon de saída:    mean (preserva sub-precisão dentro do grupo)
    agg_dict: dict = {}
    for col in value_cols:
        agg_dict[col]            = agg_method
        agg_dict[f"{col}__sd"]   = (col, lambda s: float(s.dropna().std(ddof=1))
                                              if s.dropna().size > 1 else 0.0)
        agg_dict[f"{col}__n"]    = (col, "count")
    for col in keep_cols_present:
        agg_dict[col] = "first"
    agg_dict[lat_col] = "mean"
    agg_dict[lon_col] = "mean"

    # pandas .agg com named-aggregations precisa do formato (col, func)
    # então fazemos via DataFrame.groupby().agg(**{output_name: NamedAgg(...)})
    grouped = df.groupby(["__lat_key", "__lon_key"], sort=False)
    named_aggs = {}
    for col in value_cols:
        named_aggs[col]            = pd.NamedAgg(column=col, aggfunc=agg_method)
        named_aggs[f"{col}_sd"]    = pd.NamedAgg(
            column=col,
            aggfunc=lambda s: float(s.dropna().std(ddof=1)) if s.dropna().size > 1 else 0.0,
        )
        named_aggs[f"{col}_n"]     = pd.NamedAgg(column=col, aggfunc="count")
    for col in keep_cols_present:
        named_aggs[col] = pd.NamedAgg(column=col, aggfunc="first")
    named_aggs[lat_col] = pd.NamedAgg(column=lat_col, aggfunc="mean")
    named_aggs[lon_col] = pd.NamedAgg(column=lon_col, aggfunc="mean")

    out = grouped.agg(**named_aggs).reset_index()
    out = out.drop(columns=["__lat_key", "__lon_key"])

    # Reordenar colunas: lat, lon, value_cols (+sd +n), keep_cols
    col_order = [lat_col, lon_col]
    for v in value_cols:
        col_order += [v, f"{v}_sd", f"{v}_n"]
    col_order += keep_cols_present
    out = out[col_order]

    # Stats
    reps_per_point = out[[f"{value_cols[0]}_n"]].values.flatten()
    stats = {
        "input_rows":                int(n_before),
        "input_rows_with_coords":    int(len(df)),
        "output_rows":               int(len(out)),
        "duplicates_collapsed":      int(len(df) - len(out)),
        "rows_dropped_no_coords":    int(n_dropped_coords),
        "max_replicates_per_point":  int(reps_per_point.max()) if len(reps_per_point) else 0,
        "mean_replicates_per_point": float(reps_per_point.mean()) if len(reps_per_point) else 0.0,
        "points_with_replicates":    int((reps_per_point > 1).sum()),
        "points_singleton":          int((reps_per_point == 1).sum()),
        "agg_method":                agg_method,
        "coord_precision":           coord_precision,
        "value_cols":                value_cols,
        "keep_cols_present":         keep_cols_present,
        "keep_cols_missing":         keep_cols_missing,
    }
    return out, stats


def main():
    parser = argparse.ArgumentParser(
        description="Agrega duplicatas por lat/lon antes de run_isoscape."
    )
    parser.add_argument("--job-id",          required=True)
    parser.add_argument("--input",           required=True,
                        help="CSV ou XLSX de entrada")
    parser.add_argument("--output",          required=True,
                        help="CSV de saída agregado")
    parser.add_argument("--output-dir",      required=True,
                        help="Diretório para log.txt e metrics.json")
    parser.add_argument("--lat-col",         default="latitude")
    parser.add_argument("--lon-col",         default="longitude")
    parser.add_argument("--value-cols",      required=True,
                        help="Colunas numéricas a agregar (separadas por vírgula). Ex: d13C_wood")
    parser.add_argument("--keep-cols",       default="",
                        help="Colunas a preservar (first), separadas por vírgula. Ex: Site,Family")
    parser.add_argument("--agg-method",      default="mean", choices=AGG_METHODS)
    parser.add_argument("--coord-precision", type=int, default=6,
                        help="Casas decimais para arredondar coordenadas no groupby (default 6 = ~10 cm)")

    args = parser.parse_args()

    output_dir = ensure_output_dir(Path(args.output_dir))
    logger     = setup_logging(output_dir, args.job_id, "prepare_dataset")
    metrics    = MetricsCollector(args.job_id)

    log_msg(logger, f"[→] Job prepare_dataset iniciado: {args.job_id}", "INFO")
    log_msg(logger, f"[→] Input:  {args.input}", "INFO")
    log_msg(logger, f"[→] Output: {args.output}", "INFO")
    log_msg(logger, f"[→] Método de agregação: {args.agg_method}", "INFO")
    log_msg(logger, f"[→] Precisão de coordenadas: {args.coord_precision} casas decimais", "INFO")
    log_msg(logger, "[→] Engine: Python", "INFO")

    try:
        # Ler dataset
        in_path = Path(args.input)
        if not in_path.exists():
            raise FileNotFoundError(f"Dataset não encontrado: {in_path}")
        ext = in_path.suffix.lower()
        if ext == ".csv":
            df = pd.read_csv(in_path)
        elif ext in (".xlsx", ".xls"):
            df = pd.read_excel(in_path)
        else:
            raise ValueError(f"Formato não suportado: {ext}")
        log_msg(logger, f"[✓] Dataset lido: {len(df)} linhas, {len(df.columns)} colunas", "INFO")
        log_msg(logger, f"[→] Colunas disponíveis: {', '.join(df.columns)}", "INFO")

        value_cols = parse_csv_list(args.value_cols)
        keep_cols  = parse_csv_list(args.keep_cols)
        log_msg(logger, f"[→] Colunas a agregar (value_cols): {value_cols}", "INFO")
        log_msg(logger, f"[→] Colunas a preservar (keep_cols): {keep_cols}", "INFO")

        # Agregar
        log_msg(logger, "[→] Agrupando por (lat, lon) e agregando...", "INFO")
        df_agg, stats = aggregate_duplicates(
            df,
            lat_col         = args.lat_col,
            lon_col         = args.lon_col,
            value_cols      = value_cols,
            keep_cols       = keep_cols,
            agg_method      = args.agg_method,
            coord_precision = args.coord_precision,
            logger          = logger,
        )

        # Log resumo
        log_msg(logger,
                f"[✓] Agregação concluída: {stats['input_rows']} → {stats['output_rows']} linhas "
                f"({stats['duplicates_collapsed']} duplicatas colapsadas)",
                "INFO")
        log_msg(logger,
                f"[→] Replicates por ponto: max={stats['max_replicates_per_point']}, "
                f"mean={stats['mean_replicates_per_point']:.2f}",
                "INFO")
        log_msg(logger,
                f"[→] Pontos com replicates: {stats['points_with_replicates']} "
                f"({stats['points_singleton']} singletons)",
                "INFO")

        # Distribuição de replicates (histograma resumido)
        if stats["output_rows"] > 0:
            n_col = f"{value_cols[0]}_n"
            counts = df_agg[n_col].value_counts().sort_index()
            log_msg(logger, "[→] Distribuição de replicates:", "INFO")
            for n, freq in counts.items():
                log_msg(logger, f"    {int(n):>3} medições → {int(freq):>4} árvores", "INFO")

        # Salvar
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df_agg.to_csv(out_path, index=False)
        log_msg(logger, f"[✓] CSV agregado salvo: {out_path}", "INFO")

        # Metrics
        for k, v in stats.items():
            metrics.add(k, v)
        metrics.add("engine",      "python")
        metrics.add("input_path",  str(in_path))
        metrics.add("output_path", str(out_path))
        metrics.save(output_dir / "metrics.json")
        log_msg(logger, "[✓] metrics.json salvo", "INFO")

        log_msg(logger, "[★] prepare_dataset concluído com sucesso", "INFO")
        sys.exit(0)

    except Exception as e:
        import traceback
        log_msg(logger, f"[!] Erro fatal: {e}", "ERROR")
        log_msg(logger, traceback.format_exc(), "ERROR")
        metrics.add("error", str(e))
        metrics.save(output_dir / "metrics.json")
        sys.exit(1)


if __name__ == "__main__":
    main()