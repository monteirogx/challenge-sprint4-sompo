"""Consultas analíticas e relatório consolidado de risco.

As funções `resumo_*`/`tendencia`/`estado_atual` alimentam a API e o dashboard.
`gerar_relatorio` produz um relatório HTML autocontido (para enviar ao gestor
ou à seguradora) e os gráficos PNG usados como evidência no README.
"""
import base64
import io
import json
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from sompo import banco, config  # noqa: E402
from sompo.logger import obter_logger  # noqa: E402

log = obter_logger("relatorios")

AGRUPAMENTOS = {"id_equipamento", "regiao", "tipo_operacao", "tipo_equipamento"}

# Paleta (cores de status para nível de risco; sempre acompanhadas de rótulo)
COR_NIVEL = {"BAIXO": "#0ca30c", "MODERADO": "#fab219", "ALTO": "#ec835a", "CRÍTICO": "#d03b3b"}
ORDEM_NIVEIS = ["BAIXO", "MODERADO", "ALTO", "CRÍTICO"]
CATEGORICA = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
TINTA, TINTA_2, TINTA_MUDA, GRADE, SUPERFICIE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#fcfcfb"


def carregar_risco() -> pd.DataFrame:
    df = banco.consultar("SELECT * FROM vw_risco")
    df["data_hora"] = pd.to_datetime(df["data_hora"])
    df["alerta"] = df["nivel_risco"].isin(config.NIVEIS_ALERTA)
    return df


def estado_atual(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Última leitura de cada equipamento = situação atual da frota."""
    df = carregar_risco() if df is None else df
    atual = df.sort_values("data_hora").groupby("id_equipamento").tail(1)
    return atual.sort_values("score_risco", ascending=False).reset_index(drop=True)


STATUS_ATIVOS = ("ABERTO", "RECONHECIDO")


def alertas(status: tuple[str, ...] = STATUS_ATIVOS) -> pd.DataFrame:
    """Alertas registrados (tabela `alertas`) com os detalhes da predição que os sustenta."""
    marcadores = ", ".join("?" * len(status))
    return banco.consultar(
        f"""SELECT a.id_alerta, a.status, a.nivel_risco, a.score_risco, a.id_equipamento, e.regiao, e.tipo_equipamento,
                   l.tipo_operacao, l.data_hora AS data_hora_leitura, a.aberto_em, a.atualizado_em, a.tratado_por,
                   a.observacao, p.regra_seguranca, p.fatores_json, p.recomendacao
            FROM alertas a
            JOIN equipamentos e ON e.id_equipamento = a.id_equipamento
            JOIN predicoes p ON p.id_predicao = a.id_predicao
            JOIN leituras l ON l.id_leitura = p.id_leitura
            WHERE a.status IN ({marcadores})
            ORDER BY CASE a.nivel_risco WHEN 'CRÍTICO' THEN 0 ELSE 1 END, a.score_risco DESC""", tuple(status))


def visao_geral(df: pd.DataFrame | None = None) -> dict:
    df = carregar_risco() if df is None else df
    atual = estado_atual(df)
    ultimos_7d = df[df["data_hora"] >= df["data_hora"].max() - pd.Timedelta(days=7)]
    return {
        "equipamentos_monitorados": int(atual["id_equipamento"].nunique()),
        "leituras_processadas": int(len(df)),
        "equipamentos_em_alerta": int(atual["alerta"].sum()),
        "equipamentos_criticos": int((atual["nivel_risco"] == "CRÍTICO").sum()),
        "alertas_abertos": int(banco.consultar("SELECT COUNT(*) AS n FROM alertas WHERE status = 'ABERTO'")["n"][0]),
        "alertas_reconhecidos": int(banco.consultar("SELECT COUNT(*) AS n FROM alertas WHERE status = 'RECONHECIDO'")["n"][0]),
        "score_medio_7d": round(float(ultimos_7d["score_risco"].mean()), 1) if len(ultimos_7d) else 0.0,
        "distribuicao_niveis_atual": {n: int((atual["nivel_risco"] == n).sum()) for n in ORDEM_NIVEIS},
        "ultima_leitura": df["data_hora"].max().strftime("%Y-%m-%d %H:%M") if len(df) else None,
    }


def resumo_por(agrupar_por: str, df: pd.DataFrame | None = None) -> pd.DataFrame:
    if agrupar_por not in AGRUPAMENTOS:
        raise ValueError(f"agrupar_por deve ser um de {sorted(AGRUPAMENTOS)}")
    df = carregar_risco() if df is None else df
    atual = estado_atual(df)
    resumo = df.groupby(agrupar_por).agg(
        leituras=("id_leitura", "count"),
        score_medio=("score_risco", "mean"),
        pct_leituras_em_alerta=("alerta", "mean"),
    )
    resumo["equipamentos_em_alerta_agora"] = atual.groupby(agrupar_por)["alerta"].sum()
    resumo["score_medio"] = resumo["score_medio"].round(1)
    resumo["pct_leituras_em_alerta"] = (resumo["pct_leituras_em_alerta"] * 100).round(1)
    return resumo.fillna(0).sort_values("score_medio", ascending=False).reset_index()


def tendencia(agrupar_por: str | None = None, frequencia: str = "W-MON", df: pd.DataFrame | None = None,
              min_leituras: int | None = None) -> pd.DataFrame:
    """Score médio por período (semana por padrão), opcionalmente por grupo.

    Pontos com poucas leituras (padrão: < 5; 1 quando o agrupamento é por equipamento)
    são omitidos: a média de 1 ou 2 leituras não representa uma tendência.
    """
    if min_leituras is None:
        min_leituras = 1 if agrupar_por == "id_equipamento" else 5
    df = carregar_risco() if df is None else df
    chaves = [pd.Grouper(key="data_hora", freq=frequencia)] + ([agrupar_por] if agrupar_por else [])
    serie = df.groupby(chaves).agg(score_medio=("score_risco", "mean"), pct_alerta=("alerta", "mean"),
                                   leituras=("id_leitura", "count")).reset_index()
    serie["score_medio"] = serie["score_medio"].round(1)
    serie["pct_alerta"] = (serie["pct_alerta"] * 100).round(1)
    return serie[serie["leituras"] >= min_leituras].reset_index(drop=True)


# ==========================================
# GRÁFICOS (PNG)
# ==========================================
def _estilo(ax, titulo: str, rotulo_y: str = ""):
    ax.set_title(titulo, loc="left", fontsize=12, color=TINTA, pad=12)
    ax.set_ylabel(rotulo_y, color=TINTA_2, fontsize=9)
    ax.set_facecolor(SUPERFICIE)
    for lado in ("top", "right", "left"):
        ax.spines[lado].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.tick_params(colors=TINTA_MUDA, labelsize=8, length=0)
    ax.grid(axis="y", color=GRADE, linewidth=0.8)
    ax.set_axisbelow(True)


def _figura(largura=9, altura=4):
    fig, ax = plt.subplots(figsize=(largura, altura), dpi=120)
    fig.patch.set_facecolor(SUPERFICIE)
    return fig, ax


def grafico_tendencia_operacao(df) -> plt.Figure:
    serie = tendencia("tipo_operacao", df=df)
    fig, ax = _figura()
    _estilo(ax, "Tendência semanal do score médio de risco por tipo de operação", "Score médio (0–100)")
    for i, op in enumerate(config.TIPOS_OPERACAO):
        s = serie[serie["tipo_operacao"] == op]
        if s.empty:
            continue
        ax.plot(s["data_hora"], s["score_medio"], color=CATEGORICA[i], linewidth=2, label=op)
    ax.legend(frameon=False, fontsize=8, ncol=5, loc="upper left", bbox_to_anchor=(0, -0.08))
    fig.tight_layout()
    return fig


def grafico_heatmap_regiao(df) -> plt.Figure:
    serie = tendencia("regiao", df=df)
    tabela = serie.pivot(index="regiao", columns="data_hora", values="score_medio")
    tabela = tabela.loc[tabela.mean(axis=1).sort_values(ascending=False).index]
    fig, ax = _figura(9, 3.8)
    ax.set_title("Score médio de risco por região e semana", loc="left", fontsize=12, color=TINTA, pad=12)
    img = ax.imshow(tabela.values, aspect="auto", cmap=matplotlib.colors.LinearSegmentedColormap.from_list(
        "azul", ["#cde2fb", "#6da7ec", "#2a78d6", "#1c5cab", "#0d366b"]))
    ax.set_yticks(range(len(tabela.index)), tabela.index)
    ax.set_xticks(range(0, len(tabela.columns), 2), [c.strftime("%d/%m") for c in tabela.columns[::2]])
    ax.tick_params(colors=TINTA_2, labelsize=8, length=0)
    for lado in ax.spines.values():
        lado.set_visible(False)
    barra = fig.colorbar(img, ax=ax, fraction=0.03, pad=0.02)
    barra.set_label("Score médio", color=TINTA_2, fontsize=8)
    barra.outline.set_visible(False)
    barra.ax.tick_params(labelsize=7, colors=TINTA_MUDA)
    fig.tight_layout()
    return fig


def grafico_ranking_equipamentos(df, top: int = 15) -> plt.Figure:
    atual = estado_atual(df).head(top).iloc[::-1]
    fig, ax = _figura(9, 5)
    _estilo(ax, f"Top {top} equipamentos por score de risco atual (última leitura)")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRADE, linewidth=0.8)
    cores = [COR_NIVEL[n] for n in atual["nivel_risco"]]
    ax.barh(atual["id_equipamento"] + "  " + atual["regiao"] + " · " + atual["tipo_equipamento"],
            atual["score_risco"], color=cores, height=0.7)
    for y, (s, n) in enumerate(zip(atual["score_risco"], atual["nivel_risco"])):
        ax.text(s + 1, y, f"{s:.0f} · {n}", va="center", fontsize=8, color=TINTA_2)
    ax.set_xlim(0, 115)
    ax.set_xlabel("Score de risco (probabilidade de falha em 7 dias, %)", color=TINTA_2, fontsize=9)
    ax.tick_params(axis="y", labelsize=8, colors=TINTA_2)
    fig.tight_layout()
    return fig


def grafico_distribuicao_niveis(df) -> plt.Figure:
    atual = estado_atual(df)
    contagem = [int((atual["nivel_risco"] == n).sum()) for n in ORDEM_NIVEIS]
    fig, ax = _figura(6, 3.2)
    _estilo(ax, "Situação atual da frota por nível de risco", "Equipamentos")
    ax.bar(ORDEM_NIVEIS, contagem, color=[COR_NIVEL[n] for n in ORDEM_NIVEIS], width=0.6)
    for x, c in enumerate(contagem):
        ax.text(x, c + 0.5, str(c), ha="center", fontsize=9, color=TINTA)
    fig.tight_layout()
    return fig


def grafico_comparacao_modelos(metricas: dict) -> plt.Figure:
    nomes = {"baseline_sprint2_random_forest": "Sprint 2\n(4 variáveis)", "regressao_logistica": "Regressão\nlogística",
             "random_forest": "Random\nForest", "gradient_boosting": "Gradient\nBoosting"}
    comp = metricas["comparacao_modelos"]
    chaves = list(comp)
    valores = [comp[k]["teste"]["pr_auc"] for k in chaves]
    fig, ax = _figura(7, 3.4)
    _estilo(ax, "PR-AUC no período de teste (maior é melhor)", "PR-AUC")
    cores = ["#c3c2b7" if k.startswith("baseline") else ("#2a78d6" if k == metricas["algoritmo_escolhido"] else "#86b6ef")
             for k in chaves]
    ax.bar([nomes.get(k, k) for k in chaves], valores, color=cores, width=0.6)
    for x, v in enumerate(valores):
        ax.text(x, v + 0.015, f"{v:.2f}", ha="center", fontsize=9, color=TINTA)
    ax.axhline(metricas["teste"]["taxa_base_falha"], color=TINTA_MUDA, linewidth=1, linestyle="--",
               label="Classificador aleatório (taxa base de falhas)")
    ax.legend(frameon=False, fontsize=8, loc="upper right")
    ax.set_ylim(0, 1)
    fig.tight_layout()
    return fig


def _png_base64(fig) -> str:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=fig.get_facecolor())
    return base64.b64encode(buffer.getvalue()).decode()


# ==========================================
# RELATÓRIO HTML
# ==========================================
def gerar_relatorio(salvar_evidencias: bool = True) -> str:
    df = carregar_risco()
    if df.empty:
        raise ValueError("Não há predições no banco. Execute: python main.py pipeline")
    try:
        metricas = json.loads(config.caminho_metricas().read_text(encoding="utf-8"))
    except FileNotFoundError:
        metricas = None

    graficos = {
        "distribuicao_niveis": grafico_distribuicao_niveis(df),
        "ranking_equipamentos": grafico_ranking_equipamentos(df),
        "tendencia_operacao": grafico_tendencia_operacao(df),
        "heatmap_regiao": grafico_heatmap_regiao(df),
    }
    if metricas:
        graficos["comparacao_modelos"] = grafico_comparacao_modelos(metricas)
    imagens = {}
    for nome, fig in graficos.items():
        imagens[nome] = _png_base64(fig)
        if salvar_evidencias:
            config.DIR_EVIDENCIAS.mkdir(parents=True, exist_ok=True)
            fig.savefig(config.DIR_EVIDENCIAS / f"grafico_{nome}.png", facecolor=fig.get_facecolor())
        plt.close(fig)

    geral = visao_geral(df)
    ativos = alertas()
    ativos["acao_tecnico"] = ativos["recomendacao"].map(lambda r: json.loads(r)["tecnico"])
    ativos["acao_gestor"] = ativos["recomendacao"].map(lambda r: json.loads(r)["gestor"])
    tabela_alertas = ativos[["id_alerta", "status", "id_equipamento", "regiao", "tipo_equipamento", "tipo_operacao",
                             "score_risco", "nivel_risco", "acao_tecnico", "acao_gestor"]]

    def tabela(d: pd.DataFrame) -> str:
        return d.to_html(index=False, border=0, classes="tabela", escape=True)

    criterios = "".join(f"<li><b>{n}</b>: score ≥ {m}</li>" for n, m in config.NIVEIS_RISCO)
    bloco_modelo = ""
    if metricas:
        t = metricas["teste"]
        bloco_modelo = f"""
        <h2>Modelo preditivo</h2>
        <p>Versão <code>{metricas['versao_modelo']}</code> ({metricas['algoritmo_escolhido']}), avaliado em dados de
        {metricas['periodo_teste_a_partir_de']} em diante (período nunca visto no treino).</p>
        <p>PR-AUC <b>{t['pr_auc']}</b> · ROC-AUC <b>{t['roc_auc']}</b> · Brier <b>{t['brier']}</b> ·
        No nível ALTO: recall <b>{t['recall_alerta']:.0%}</b>, precisão <b>{t['precisao_alerta']:.0%}</b>.</p>
        <img src="data:image/png;base64,{imagens['comparacao_modelos']}" alt="Comparação de modelos">"""

    html = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relatório de Risco da Frota</title>
<style>
 body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background:#f9f9f7; color:#0b0b0b;
        max-width: 980px; margin: 0 auto; padding: 24px 16px; }}
 h1 {{ font-size: 1.6rem; margin-bottom: 4px; }} h2 {{ margin-top: 36px; font-size: 1.2rem; }}
 .sub {{ color:#52514e; }} img {{ max-width:100%; height:auto; border:1px solid #e1e0d9; border-radius:8px; }}
 .kpis {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap:12px; margin-top:16px; }}
 .kpi {{ background:#fcfcfb; border:1px solid #e1e0d9; border-radius:8px; padding:12px; }}
 .kpi b {{ display:block; font-size:1.6rem; }} .kpi span {{ color:#52514e; font-size:.85rem; }}
 .tabela {{ border-collapse: collapse; width:100%; font-size:.82rem; background:#fcfcfb; }}
 .tabela th, .tabela td {{ padding:6px 8px; border-bottom:1px solid #e1e0d9; text-align:left; vertical-align:top; }}
 .rolagem {{ overflow-x:auto; }}
</style></head><body>
<h1>Relatório de Risco da Frota — Sompo Seguros</h1>
<p class="sub">Gerado em {datetime.now():%d/%m/%Y %H:%M} · Última leitura: {geral['ultima_leitura']} · Sistema v{config.VERSAO_SISTEMA}</p>
<div class="kpis">
 <div class="kpi"><b>{geral['equipamentos_monitorados']}</b><span>equipamentos monitorados</span></div>
 <div class="kpi"><b>{geral['leituras_processadas']:,}</b><span>leituras processadas</span></div>
 <div class="kpi"><b>{geral['equipamentos_em_alerta']}</b><span>em alerta (ALTO/CRÍTICO)</span></div>
 <div class="kpi"><b>{geral['equipamentos_criticos']}</b><span>em nível CRÍTICO</span></div>
 <div class="kpi"><b>{geral['score_medio_7d']}</b><span>score médio (últimos 7 dias)</span></div>
</div>
<h2>Critérios dos níveis</h2>
<p>Score = probabilidade estimada de falha nos próximos 7 dias (0–100). Regras de segurança (ex.: motor ≥ 110 °C)
forçam CRÍTICO independentemente do modelo.</p><ul>{criterios}</ul>
<h2>Situação atual</h2>
<img src="data:image/png;base64,{imagens['distribuicao_niveis']}" alt="Distribuição por nível">
<img src="data:image/png;base64,{imagens['ranking_equipamentos']}" alt="Ranking de equipamentos">
<h2>Alertas ativos e recomendações</h2>
<div class="rolagem">{tabela(tabela_alertas) if len(tabela_alertas) else '<p>Nenhum alerta ativo.</p>'}</div>
<h2>Tendências</h2>
<img src="data:image/png;base64,{imagens['tendencia_operacao']}" alt="Tendência por operação">
<img src="data:image/png;base64,{imagens['heatmap_regiao']}" alt="Heatmap por região">
<h2>Resumo por região</h2><div class="rolagem">{tabela(resumo_por('regiao', df))}</div>
<h2>Resumo por tipo de operação</h2><div class="rolagem">{tabela(resumo_por('tipo_operacao', df))}</div>
<h2>Resumo por tipo de equipamento</h2><div class="rolagem">{tabela(resumo_por('tipo_equipamento', df))}</div>
{bloco_modelo}
</body></html>"""

    config.DIR_RELATORIOS.mkdir(parents=True, exist_ok=True)
    caminho = config.DIR_RELATORIOS / "relatorio_risco.html"
    caminho.write_text(html, encoding="utf-8")
    log.info("Relatório gerado em %s", caminho)
    return str(caminho)
