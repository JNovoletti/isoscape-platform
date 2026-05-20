#!/usr/bin/env python
"""
inspect_duplicates.py

Mostra espécies que compartilham o mesmo ponto (lat/lon) em madeiras.csv.
Útil para decidir o group_cols correto antes de rodar prepare_dataset.

Uso:
    python inspect_duplicates.py /data/datasets/madeiras.csv
    python inspect_duplicates.py /data/datasets/madeiras.csv --precision 4
    python inspect_duplicates.py /data/datasets/madeiras.csv --col Scientific_name
    python inspect_duplicates.py /data/datasets/madeiras.csv --top 20
"""

import sys
import argparse
from pathlib import Path

import pandas as pd
import numpy as np


def main():
    parser = argparse.ArgumentParser(
        description="Inspeciona espécies no mesmo ponto lat/lon em um CSV de amostras."
    )
    parser.add_argument("input", help="Caminho do CSV (ex: /data/datasets/madeiras.csv)")
    parser.add_argument("--lat-col",   default="latitude")
    parser.add_argument("--lon-col",   default="longitude")
    parser.add_argument("--col",       default="Scientific_name",
                        help="Coluna de identidade da espécie (default: Scientific_name)")
    parser.add_argument("--precision", type=int, default=6,
                        help="Casas decimais para arredondar coords (default 6 = ~10 cm)")
    parser.add_argument("--top",       type=int, default=None,
                        help="Mostrar apenas os N pontos com mais espécies (default: todos)")
    args = parser.parse_args()

    # ── Ler CSV ────────────────────────────────────────────────────────────────
    p = Path(args.input)
    if not p.exists():
        print(f"[!] Arquivo não encontrado: {p}")
        sys.exit(1)

    df = pd.read_csv(p, low_memory=False)
    print(f"\n{'='*60}")
    print(f"Arquivo:  {p.name}")
    print(f"Linhas:   {len(df)}")
    print(f"Colunas:  {len(df.columns)}")
    print(f"{'='*60}\n")

    # Verificar colunas necessárias
    for c in [args.lat_col, args.lon_col, args.col]:
        if c not in df.columns:
            # Tentar case-insensitive
            match = [x for x in df.columns if x.lower() == c.lower()]
            if match:
                print(f"[→] Coluna '{c}' → usando '{match[0]}' (case-insensitive)")
                if c == args.lat_col:   args.lat_col = match[0]
                elif c == args.lon_col: args.lon_col = match[0]
                else:                  args.col = match[0]
            else:
                print(f"[!] Coluna '{c}' não encontrada.")
                print(f"    Disponíveis: {', '.join(df.columns)}")
                sys.exit(1)

    # ── Arredondar coords ─────────────────────────────────────────────────────
    df["__lat"] = pd.to_numeric(df[args.lat_col], errors="coerce").round(args.precision)
    df["__lon"] = pd.to_numeric(df[args.lon_col], errors="coerce").round(args.precision)
    df = df.dropna(subset=["__lat", "__lon"])

    # ── Quantas espécies distintas por ponto ──────────────────────────────────
    point_species = (
        df.groupby(["__lat", "__lon"])[args.col]
        .agg(lambda s: s.dropna().unique().tolist())
        .reset_index()
    )
    point_species.columns = ["lat", "lon", "species_list"]
    point_species["n_species"]  = point_species["species_list"].apply(len)
    point_species["n_rows"]     = (
        df.groupby(["__lat", "__lon"]).size().values
    )

    # ── Resumo geral ──────────────────────────────────────────────────────────
    n_points       = len(point_species)
    n_multi        = (point_species["n_species"] > 1).sum()
    n_single       = (point_species["n_species"] == 1).sum()
    max_sp         = int(point_species["n_species"].max())

    print(f"Coluna de espécie:  {args.col}")
    print(f"Precisão de coord:  {args.precision} casas decimais")
    print(f"Total de pontos:    {n_points}")
    print(f"  → ponto único (1 espécie):          {n_single}")
    print(f"  → ponto compartilhado (>1 espécie): {n_multi}")
    print(f"  → máx espécies num mesmo ponto:     {max_sp}")

    if n_multi == 0:
        print("\n✓ Nenhum ponto compartilhado entre espécies diferentes.")
        print("  Você pode usar group_cols=[\"Scientific_name\"] sem preocupação.")
        return

    # ── Pontos com mais de uma espécie ────────────────────────────────────────
    multi = (
        point_species[point_species["n_species"] > 1]
        .sort_values("n_species", ascending=False)
        .reset_index(drop=True)
    )

    if args.top:
        multi = multi.head(args.top)
        print(f"\n{'─'*60}")
        print(f"TOP {args.top} pontos com mais espécies distintas:")
    else:
        print(f"\n{'─'*60}")
        print(f"Todos os {n_multi} pontos com >1 espécie:")

    print(f"{'─'*60}")

    for _, row in multi.iterrows():
        lat, lon = row["lat"], row["lon"]
        sp_list  = row["species_list"]
        n_sp     = row["n_species"]
        n_rows   = row["n_rows"]

        print(f"\nPonto ({lat:.{args.precision}f}, {lon:.{args.precision}f})"
              f"  —  {n_sp} espécies, {n_rows} linhas no CSV")

        # Detalhe por espécie nesse ponto
        mask = (df["__lat"] == lat) & (df["__lon"] == lon)
        sub  = df[mask]
        sp_detail = sub.groupby(args.col, dropna=False).agg(
            n_linhas    = (args.lat_col,    "count"),
        ).reset_index()

        # Adicionar colunas isotópicas se existirem
        isotope_cols = [c for c in df.columns
                        if any(c.startswith(p) for p in
                               ["d13C", "d15N", "d18O", "Sr", "C_", "N_"])]
        if isotope_cols:
            iso_detail = sub.groupby(args.col, dropna=False)[isotope_cols].agg(
                lambda s: f"{s.notna().sum()}/{len(s)} válidos"
            ).reset_index()
            sp_detail = sp_detail.merge(iso_detail, on=args.col, how="left")

        for _, sr in sp_detail.iterrows():
            sp_name = sr[args.col]
            n_lin   = sr["n_linhas"]
            iso_str = ""
            if isotope_cols:
                parts = [f"{c}={sr[c]}" for c in isotope_cols[:4]]  # top 4
                iso_str = "  |  " + "  ".join(parts)
            print(f"    {str(sp_name):<40}  {n_lin} linha(s){iso_str}")

    # ── Distribuição de n_species por ponto ───────────────────────────────────
    print(f"\n{'─'*60}")
    print("Distribuição: quantos pontos têm N espécies distintas")
    print(f"{'─'*60}")
    dist = point_species["n_species"].value_counts().sort_index()
    for n, cnt in dist.items():
        bar = "█" * min(cnt, 40)
        print(f"  {int(n):>2} espécie(s) → {cnt:>5} pontos  {bar}")

    # ── Recomendação ──────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("Recomendação para prepare_dataset:")
    print(f"{'─'*60}")
    if n_multi > 0:
        print(f'  group_cols = ["{args.col}"]')
        print()
        print("  Isso garante que espécies diferentes no mesmo ponto")
        print("  ficam em linhas separadas no CSV agregado.")
    print()


if __name__ == "__main__":
    main()