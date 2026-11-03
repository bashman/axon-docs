# Paper / Paper

Coste de inferencia por tarea verificada en AXON. / Inference cost per verified task in AXON.

| | Español | English |
|---|---|---|
| LaTeX | `axon-paper.es.tex` | `axon-paper.en.tex` |
| PDF | `axon-paper.es.pdf` | `axon-paper.en.pdf` |
| Markdown | `axon-paper.es.md` | `axon-paper.en.md` |
| Figuras / Figures | `figs/es/` | `figs/en/` |

## Compilar / Build

Requiere TeX Live (`texlive-latex-extra`, `texlive-lang-spanish`). Desde esta carpeta, dos pasadas para resolver referencias. / Requires TeX Live. From this folder, two passes to resolve references.

```bash
cd papers/inference-cost
pdflatex axon-paper.es.tex && pdflatex axon-paper.es.tex
pdflatex axon-paper.en.tex && pdflatex axon-paper.en.tex
```

Los archivos auxiliares (`.aux`, `.log`, `.out`) se pueden borrar. / Auxiliary files can be deleted.

## Figuras / Figures

PDF vectoriales para LaTeX y PNG para Markdown, en español e inglés. Desde la raíz del repositorio: / Vector PDFs for LaTeX and PNGs for Markdown, in Spanish and English. From the repository root:

```bash
uv run --no-project --with matplotlib python papers/inference-cost/figs/make_figs.py
```

`figs/` debe quedar junto a los `.tex`. / `figs/` must sit next to the `.tex` files.
