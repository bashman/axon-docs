"""Generate the paper figures (vector PDF for LaTeX, PNG for Markdown) in Spanish and English.

Run from the repository root:
    uv run --no-project --with matplotlib python docs/paper/figs/make_figs.py
Output: docs/paper/figs/{es,en}/fig_*.{pdf,png}
"""
from pathlib import Path

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

HERE = Path(__file__).parent
BLUE, ORANGE, GREEN, GRAY = "#dbe7f7", "#fbd9b4", "#d4ecd0", "#e6e6e6"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "pdf.fonttype": 42})

T = {
    "es": {
        "dec": ",",
        "person": "Persona\nspec ejecutable\n(solo lectura)", "agent": "Agente\n(modelo)",
        "mcp": "Herramientas\nMCP", "proj": "Proyecciones\nTypeScript /\npseudocódigo",
        "graph": "Grafo Merkle\ntipado (BLAKE3)", "verifier": "Verificador\ntipos y efectos",
        "contract": "contrato", "defines": "define /\nconsulta", "mutation": "mutación",
        "accepts": "acepta", "projects": "proyecta", "rejects": "rechaza:\nerror estructurado",
        "review": "revisión",
        "panel_a": "(a) Tokens por tarea", "panel_b": "(b) Campaña global",
        "tokens": "Tokens", "cost": "Coste", "price": "Precio\npor token",
        "ratio_y": "razón AXON / baseline (menor es mejor)",
        "ft": "Ajuste fino\nsobre AXON", "prefix": "Prefijo $F$\n(instrucciones)",
        "iters": "Iteraciones $T$\n(reparaciones)", "syntax": "Errores de sintaxis\n(éxito $p$)",
        "size": "Tamaño del modelo\n(precio $c$)", "cost_per": "Coste por tarea\nverificada\n",
        "measured": "medido (campaña)",
        "sens_x": r"precio efectivo por token $\rho$ (relativo al baseline)",
        "sens_y": "coste por tarea verificada",
        "untuned": "Sin ajustar", "tuned": "Ajustado",
        "A": "(A) referencia", "B": "(B) efecto del entrenamiento\nsin la interfaz",
        "C": "(C) efecto de la interfaz\nsin entrenamiento", "D": "(D) efecto combinado\n(contraste de H1–H3)",
    },
    "en": {
        "dec": ".",
        "person": "Person\nexecutable spec\n(read-only)", "agent": "Agent\n(model)",
        "mcp": "MCP\ntools", "proj": "Projections\nTypeScript /\npseudocode",
        "graph": "Typed Merkle\ngraph (BLAKE3)", "verifier": "Verifier\ntypes and effects",
        "contract": "contract", "defines": "defines /\nqueries", "mutation": "mutation",
        "accepts": "accepts", "projects": "projects", "rejects": "rejects:\nstructured error",
        "review": "review",
        "panel_a": "(a) Tokens per task", "panel_b": "(b) Overall campaign",
        "tokens": "Tokens", "cost": "Cost", "price": "Price\nper token",
        "ratio_y": "AXON / baseline ratio (lower is better)",
        "ft": "Fine-tuning\non AXON", "prefix": "Prefix $F$\n(instructions)",
        "iters": "Iterations $T$\n(repairs)", "syntax": "Syntax errors\n(success $p$)",
        "size": "Model size\n(price $c$)", "cost_per": "Cost per verified\ntask\n",
        "measured": "measured (campaign)",
        "sens_x": r"effective price per token $\rho$ (relative to baseline)",
        "sens_y": "cost per verified task",
        "untuned": "Untuned", "tuned": "Fine-tuned",
        "A": "(A) reference", "B": "(B) training effect\nwithout the interface",
        "C": "(C) interface effect\nwithout training", "D": "(D) combined effect\n(contrast for H1–H3)",
    },
}


def canvas(w, h, xmax, ymax):
    fig, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, xmax)
    ax.set_ylim(0, ymax)
    ax.axis("off")
    return fig, ax


def box(ax, cx, cy, w, h, text, color):
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h, boxstyle="round,pad=0.02,rounding_size=0.12",
                                fc=color, ec="black", lw=0.9))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=8.5)


def arrow(ax, p, q, label=None, color="black", ls="-", lpos=0.5):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=11, lw=1.3, color=color, ls=ls))
    if label:
        ax.text(p[0] + (q[0] - p[0]) * lpos, p[1] + (q[1] - p[1]) * lpos, label, ha="center", va="center",
                fontsize=7, color=color, bbox=dict(fc="white", ec="none", pad=1))


def num(v, t, nd=2):
    return f"{v:.{nd}f}".replace(".", t["dec"])


def save(fig, out, name):
    fig.savefig(out / f"{name}.pdf", bbox_inches="tight")
    fig.savefig(out / f"{name}.png", bbox_inches="tight", dpi=200)
    plt.close(fig)


def arch(t, out):
    fig, ax = canvas(7.2, 3.6, 10, 5)
    w, h, top, bot = 2.3, 1.1, 4.0, 1.0
    xs = (1.4, 5.0, 8.6)
    box(ax, xs[0], top, w, h, t["person"], BLUE)
    box(ax, xs[1], top, w, h, t["agent"], ORANGE)
    box(ax, xs[2], top, w, h, t["mcp"], GREEN)
    box(ax, xs[0], bot, w, h, t["proj"], BLUE)
    box(ax, xs[1], bot, w, h, t["graph"], GREEN)
    box(ax, xs[2], bot, w, h, t["verifier"], GREEN)
    arrow(ax, (xs[0] + w / 2, top), (xs[1] - w / 2, top), t["contract"])
    arrow(ax, (xs[1] + w / 2, top), (xs[2] - w / 2, top), t["defines"])
    arrow(ax, (xs[2], top - h / 2), (xs[2], bot + h / 2), t["mutation"])
    arrow(ax, (xs[2] - w / 2, bot), (xs[1] + w / 2, bot), t["accepts"])
    arrow(ax, (xs[1] - w / 2, bot), (xs[0] + w / 2, bot), t["projects"])
    arrow(ax, (xs[2] - w / 2, bot + h / 2), (xs[1] + w / 2 - 0.2, top - h / 2), t["rejects"], "#a02020", "--")
    arrow(ax, (xs[0], top - h / 2), (xs[0], bot + h / 2), t["review"], "#2a4d8f", "--")
    save(fig, out, "fig_arch")


def results(t, out):
    fig, (a, b) = plt.subplots(1, 2, figsize=(7.2, 3.0), gridspec_kw={"width_ratios": [5, 3]})
    vals = [0.57, 0.78, 0.67, 1.38, 1.05]
    bars = a.bar(["T2", "T3-100", "T3-500", "T4", "T5"], vals, color=["#4c9a4c" if v < 1 else "#d46a6a" for v in vals])
    a.bar_label(bars, labels=[num(v, t) for v in vals], fontsize=8, padding=2)
    a.set_title(t["panel_a"], fontsize=9)
    gv = [0.844, 1.014, 1.20]
    b2 = b.bar([t["tokens"], t["cost"], t["price"]], gv, color="#8a8a8a")
    b.bar_label(b2, labels=[num(v, t) for v in gv], fontsize=8, padding=2)
    b.set_title(t["panel_b"], fontsize=9)
    for ax in (a, b):
        ax.axhline(1, ls="--", color="black", lw=1)
        ax.set_ylim(0, 1.6)
        ax.spines[["top", "right"]].set_visible(False)
    a.set_ylabel(t["ratio_y"])
    if t["dec"] == ",":
        for ax in (a, b):
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.1f}".replace(".", ",")))
    fig.tight_layout()
    save(fig, out, "fig_resultados")


def levers(t, out):
    fig, ax = canvas(7.2, 3.6, 10, 5.4)
    box(ax, 1.2, 2.5, 2.0, 1.0, t["ft"], ORANGE)
    for y, key in ((4.6, "prefix"), (3.4, "iters"), (2.0, "syntax"), (0.8, "size")):
        box(ax, 4.3, y, 2.7, 0.9, t[key], BLUE)
        arrow(ax, (2.2, 2.5), (2.95, y))
    for y, sym in ((4.0, r"$\tau$"), (2.0, r"$\sigma$"), (0.8, r"$\rho$")):
        box(ax, 6.9, y, 0.9, 0.8, sym, GRAY)
    arrow(ax, (5.65, 4.6), (6.45, 4.1))
    arrow(ax, (5.65, 3.4), (6.45, 3.9))
    arrow(ax, (5.65, 2.0), (6.45, 2.0))
    arrow(ax, (5.65, 0.8), (6.45, 0.8))
    box(ax, 9.0, 2.4, 1.9, 1.5, t["cost_per"] + r"$\rho\,\tau/\sigma$", GREEN)
    for y in (4.0, 2.0, 0.8):
        arrow(ax, (7.35, y), (8.05, 2.4))
    save(fig, out, "fig_palancas")


def sens(t, out):
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    xs = [0, 1.4]
    for tau, sg, c in ((1.0, 1.0, "#2a4d8f"), (0.7, 1.0, "#d08a2a"), (0.7, 0.8, "#3c8a3c")):
        label = rf"$\tau={num(tau, t, 1)};\ \sigma={num(sg, t, 1)}$".replace(",", "{,}")
        ax.plot(xs, [tau * x / sg for x in xs], lw=2, color=c, label=label)
    ax.axhline(1, ls="--", color="black", lw=1)
    ax.plot([1.2], [1.014], "o", color="red", ms=7)
    ax.annotate(t["measured"], (1.2, 1.014), (0.74, 1.16), color="red", fontsize=8,
                arrowprops=dict(arrowstyle="-", color="red"))
    ax.set_xlim(0, 1.4)
    ax.set_ylim(0, 1.3)
    ax.set_xlabel(t["sens_x"])
    ax.set_ylabel(t["sens_y"])
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    if t["dec"] == ",":
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.1f}".replace(".", ",")))
    fig.tight_layout()
    save(fig, out, "fig_sens")


def design(t, out):
    fig, ax = canvas(6.4, 2.8, 10, 4.4)
    ax.text(3.6, 4.0, t["untuned"], ha="center", fontweight="bold")
    ax.text(7.6, 4.0, t["tuned"], ha="center", fontweight="bold")
    ax.text(1.0, 2.7, "TypeScript", ha="center", va="center", rotation=90, fontweight="bold")
    ax.text(1.0, 0.9, "AXON", ha="center", va="center", rotation=90, fontweight="bold")
    box(ax, 3.6, 2.7, 3.6, 1.4, t["A"], GRAY)
    box(ax, 7.6, 2.7, 3.6, 1.4, t["B"], BLUE)
    box(ax, 3.6, 0.9, 3.6, 1.4, t["C"], BLUE)
    box(ax, 7.6, 0.9, 3.6, 1.4, t["D"], ORANGE)
    save(fig, out, "fig_diseno")


if __name__ == "__main__":
    for lang, t in T.items():
        out = HERE / lang
        out.mkdir(exist_ok=True)
        for f in (arch, results, levers, sens, design):
            f(t, out)
        print(lang, sorted(p.name for p in out.iterdir()))
