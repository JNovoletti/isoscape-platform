#!/usr/bin/env python
"""
backend/python_scripts/prepare_dataset.py

Pré-processamento de dataset de amostras para o pipeline de isoscape.

Agrega réplicas por (lat, lon, Scientific_name) — ou outro grupo configurável.
Output tem APENAS: lat, lon, group_cols, value_cols + _sd + _n.
Todo o resto do CSV original (Point, Site, map, mat, etc.) é descartado.

NA policy:
  - medições NA são ignoradas no cálculo da média
  - se TODAS as medições de um grupo forem NA → valor=NA, _sd=NA, _n=0

Uso:
    python prepare_dataset.py \\
      --job-id abc-123 \\
      --input  /data/datasets/madeiras.csv \\
      --output /data/datasets/madeiras_aggregated.csv \\
      --output-dir /data/prepared/abc-123/ \\
      --lat-col latitude \\
      --lon-col longitude \\
      --group-cols Scientific_name \\
      --value-cols d13C_wood,d15N_wood,d18O \\
      --agg-method mean \\
      --coord-precision 6
"""

import argparse
import sys
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from python_scripts.utils import (
    setup_logging, log_msg, MetricsCollector, ensure_output_dir, parse_csv_list,
)


AGG_METHODS = ("mean", "median", "min", "max", "sum")


# =============================================================================
# Funções de agregação por série
# =============================================================================

def _agg_value(s: pd.Series, method: str):
    """Agrega ignorando NA. Retorna NA se tudo NA."""
    vals = s.dropna()
    if vals.empty:
        return np.nan
    return getattr(vals, method)()


def _agg_sd(s: pd.Series):
    """Desvio padrão amostral (ddof=1). NA se n<=1 ou tudo NA."""
    vals = s.dropna()
    if vals.size <= 1:
        return np.nan
    return float(vals.std(ddof=1))


def _agg_n(s: pd.Series) -> int:
    """Conta não-NA. 0 se tudo NA."""
    return int(s.dropna().size)


# =============================================================================
# Agregação principal
# =============================================================================

def aggregate_duplicates(
    df: pd.DataFrame,
    lat_col: str,
    lon_col: str,
    group_cols: List[str],
    value_cols: List[str],
    agg_method: str = "mean",
    coord_precision: int = 6,
    logger=None,
) -> Tuple[pd.DataFrame, dict]:
    """
    Colapsa réplicas por (lat_col, lon_col, *group_cols).

    Output tem APENAS: lat, lon, group_cols, value_cols (+ _sd + _n).
    Todo o resto é descartado.
    """
    if agg_method not in AGG_METHODS:
        raise ValueError(f"agg_method inválido: {agg_method!r}. Use: {AGG_METHODS}")

    # Validar colunas obrigatórias
    all_needed = [lat_col, lon_col] + group_cols + value_cols
    missing = [c for c in all_needed if c not in df.columns]
    if missing:
        raise ValueError(f"Colunas ausentes no dataset: {missing}")

    # Matching case-insensitive para group_cols
    col_lower = {c.lower(): c for c in df.columns}
    resolved_group = []
    for g in group_cols:
        real = col_lower.get(g.lower())
        if real is None:
            raise ValueError(
                f"Coluna de grupo não encontrada (mesmo ignorando capitalização): {g!r}"
            )
        resolved_group.append(real)
        if real != g and logger is not None:
            log_msg(logger, f"[→] group_col {g!r} → {real!r} (case-insensitive)", "INFO")

    # Selecionar APENAS as colunas necessárias — resto é descartado aqui
    cols_needed = [lat_col, lon_col] + resolved_group + value_cols
    df = df[cols_needed].copy()

    # Converter coords para numérico
    df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")
    df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")

    # Converter value_cols para numérico
    for col in value_cols:
        before = df[col].isna().sum()
        df[col] = pd.to_numeric(df[col], errors="coerce")
        new_na = df[col].isna().sum() - before
        if new_na > 0 and logger is not None:
            log_msg(logger, f"[!] {col}: {new_na} valores não-numéricos → NA", "WARNING")

    # Chaves de agrupamento arredondadas (sem mexer nas originais)
    n_before = len(df)
    df["__lat_key"] = df[lat_col].round(coord_precision)
    df["__lon_key"] = df[lon_col].round(coord_precision)
    df = df.dropna(subset=["__lat_key", "__lon_key"]).reset_index(drop=True)
    n_dropped = n_before - len(df)
    if n_dropped > 0 and logger is not None:
        log_msg(logger, f"[!] {n_dropped} linhas descartadas por coordenada inválida", "WARNING")

    # Chave de agrupamento completa (chaves internas + group_cols reais)
    group_keys = ["__lat_key", "__lon_key"] + resolved_group

    # ── Agregação em um único groupby ────────────────────────────────────────
    # Montamos um dicionário de agregação para todas as colunas de uma vez,
    # evitando múltiplos merges que causavam o conflito de nome no reset_index.

    agg_spec = {}

    # Lat/lon: média dentro do grupo (preserva sub-precisão)
    agg_spec[lat_col] = (lat_col, "mean")
    agg_spec[lon_col] = (lon_col, "mean")

    # group_cols: first (constantes dentro do grupo)
    for g in resolved_group:
        agg_spec[g] = (g, "first")

    # value_cols: valor + _sd + _n usando funções lambda
    m = agg_method
    for col in value_cols:
        agg_spec[col]          = (col, lambda s, m=m: _agg_value(s, m))
        agg_spec[f"{col}_sd"]  = (col, _agg_sd)
        agg_spec[f"{col}_n"]   = (col, _agg_n)

    result = df.groupby(group_keys, sort=False, dropna=False).agg(**{
        k: pd.NamedAgg(column=v[0], aggfunc=v[1])
        for k, v in agg_spec.items()
    }).reset_index(drop=True)

    # Reordenar colunas: lat, lon, group_cols, value_cols (+sd +n)
    col_order = [lat_col, lon_col] + resolved_group
    for v in value_cols:
        col_order += [v, f"{v}_sd", f"{v}_n"]
    result = result[col_order].reset_index(drop=True)

    # Stats
    first_n = f"{value_cols[0]}_n"
    reps = result[first_n].to_numpy(dtype=float)
    stats = {
        "input_rows":                int(n_before),
        "input_rows_with_coords":    int(len(df)),
        "output_rows":               int(len(result)),
        "duplicates_collapsed":      int(len(df) - len(result)),
        "rows_dropped_no_coords":    int(n_dropped),
        "max_replicates_per_point":  int(np.nanmax(reps)) if reps.size else 0,
        "mean_replicates_per_point": float(np.nanmean(reps)) if reps.size else 0.0,
        "points_with_replicates":    int((reps > 1).sum()),
        "points_singleton":          int((reps == 1).sum()),
        "points_all_na":             int((reps == 0).sum()),
        "agg_method":                agg_method,
        "coord_precision":           coord_precision,
        "group_cols":                resolved_group,
        "value_cols":                value_cols,
    }
    return result, stats


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Agrega réplicas por (lat, lon, espécie) antes de run_isoscape."
    )
    parser.add_argument("--job-id",          required=True)
    parser.add_argument("--input",           required=True)
    parser.add_argument("--output",          required=True)
    parser.add_argument("--output-dir",      required=True)
    parser.add_argument("--lat-col",         default="latitude")
    parser.add_argument("--lon-col",         default="longitude")
    parser.add_argument("--group-cols",      default="Scientific_name",
                        help="Colunas de identidade do organismo além de lat/lon. "
                             "Separe por vírgula. Default: Scientific_name")
    parser.add_argument("--value-cols",      required=True,
                        help="Colunas isotópicas a agregar. Ex: d13C_wood,d15N_wood,d18O")
    parser.add_argument("--agg-method",      default="mean", choices=AGG_METHODS)
    parser.add_argument("--coord-precision", type=int, default=6)

    args = parser.parse_args()

    output_dir = ensure_output_dir(Path(args.output_dir))
    logger     = setup_logging(output_dir, args.job_id, "prepare_dataset")
    metrics    = MetricsCollector(args.job_id)

    log_msg(logger, f"[→] Job prepare_dataset iniciado: {args.job_id}", "INFO")
    log_msg(logger, f"[→] Input:    {args.input}", "INFO")
    log_msg(logger, f"[→] Output:   {args.output}", "INFO")
    log_msg(logger, f"[→] Método:   {args.agg_method}", "INFO")
    log_msg(logger, f"[→] Precisão: {args.coord_precision} casas decimais", "INFO")
    log_msg(logger, "[→] Engine: Python", "INFO")

    try:
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

        group_cols = parse_csv_list(args.group_cols, default=["Scientific_name"])
        value_cols = parse_csv_list(args.value_cols)

        log_msg(logger,
                f"[→] Agrupando por: ({args.lat_col}, {args.lon_col}, {', '.join(group_cols)})",
                "INFO")
        log_msg(logger, f"[→] Isotópicas a agregar: {', '.join(value_cols)}", "INFO")
        log_msg(logger,
                "[→] Demais colunas (Point, Site, map, mat, etc.) serão descartadas do output",
                "INFO")

        df_agg, stats = aggregate_duplicates(
            df,
            lat_col         = args.lat_col,
            lon_col         = args.lon_col,
            group_cols      = group_cols,
            value_cols      = value_cols,
            agg_method      = args.agg_method,
            coord_precision = args.coord_precision,
            logger          = logger,
        )

        log_msg(logger,
                f"[✓] Agregação concluída: {stats['input_rows']} → {stats['output_rows']} linhas "
                f"({stats['duplicates_collapsed']} réplicas colapsadas)",
                "INFO")
        log_msg(logger,
                f"[→] Réplicas por grupo: max={stats['max_replicates_per_point']} | "
                f"mean={stats['mean_replicates_per_point']:.2f} | "
                f"tudo NA: {stats['points_all_na']} grupos",
                "INFO")

        # Histograma de réplicas
        if stats["output_rows"] > 0:
            first_n_col = f"{value_cols[0]}_n"
            counts = df_agg[first_n_col].value_counts().sort_index()
            log_msg(logger, f"[→] Distribuição de réplicas válidas ({value_cols[0]}):", "INFO")
            for n, freq in counts.items():
                label = " ← tudo NA" if n == 0 else ""
                log_msg(logger, f"    {int(n):>3} medições → {int(freq):>4} grupos{label}", "INFO")

        # Cobertura por isotópica
        log_msg(logger, "[→] Cobertura por coluna isotópica:", "INFO")
        for col in value_cols:
            total    = len(df_agg)
            com_dado = int((df_agg[f"{col}_n"] > 0).sum())
            pct      = 100.0 * com_dado / total if total > 0 else 0.0
            log_msg(logger,
                    f"    {col:<22} {com_dado:>4}/{total} grupos com dados ({pct:.1f}%)",
                    "INFO")

        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df_agg.to_csv(out_path, index=False)
        log_msg(logger, f"[✓] CSV agregado salvo: {out_path}", "INFO")
        log_msg(logger,
                f"[→] Colunas do output: {', '.join(df_agg.columns.tolist())}",
                "INFO")

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