"""
backend/python_scripts/

Python equivalents of R scripts for geospatial data processing, isoscape model
training, and origin assignment.

Provides:
  - gen_rasters.py  — equivalente ao 01_worldclim.R (download WorldClim + recorte)
  - run_isoscape.py — equivalente aos 02_extr_dados_raster.R + 03_integracao_ML.R
                      (extração de variáveis + Random Forest + isoscape + incerteza)
  - run_assign.py   — equivalente ao 04_assign.R
                      (atribuição bayesiana de origem geográfica)
  - utils.py        — utilitários compartilhados (logging, métricas, I/O)

Convenções:
  - Outputs Python usam sufixo "_py" (ex: isoscape_py.tif)
  - Outputs R usam sufixo "_r"   (ex: isoscape_r.tif)
  - WorldClim cache é compartilhado entre R e Python:
      {worldclim_dir}/climate/wc2.1_{res}m/wc2.1_{res}m_{var}_{NN}.tif
"""