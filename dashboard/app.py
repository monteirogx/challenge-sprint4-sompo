"""Dashboard do MVP (Streamlit). Execução: python main.py dashboard

O acesso exige a chave do perfil; cada perfil vê apenas as abas que sua
permissão libera (mesmas regras da API). Toda ação relevante vai para a auditoria.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sompo import banco, config, modelo, relatorios, servico  # noqa: E402
from sompo.seguranca import AcessoNegado, autenticar  # noqa: E402

st.set_page_config(page_title="Sompo — Risco de Quebra", page_icon="🚜", layout="wide")

COR_NIVEL = relatorios.COR_NIVEL
ICONE_NIVEL = {"BAIXO": "🟢", "MODERADO": "🟡", "ALTO": "🟠", "CRÍTICO": "🔴"}
CATEGORICA = relatorios.CATEGORICA
LAYOUT = dict(margin=dict(l=10, r=10, t=40, b=10), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
              hovermode="x unified", legend=dict(orientation="h", y=-0.2), font=dict(size=12))
TITULO_PERFIL = {"operador": "Operador de máquinas", "tecnico": "Técnico de manutenção", "gestor": "Gestor de frota",
                 "analista": "Analista da seguradora", "admin": "Administrador"}


# ==========================================
# LOGIN
# ==========================================
def tela_login():
    st.title("🚜 Sompo Seguros — Prevenção de Quebra de Maquinário")
    st.caption("MVP Sprint 4 · acesso restrito por perfil")
    with st.form("login"):
        chave = st.text_input("Chave de acesso", type="password", help="Chave do seu perfil (definida no .env)")
        if st.form_submit_button("Entrar", type="primary"):
            try:
                usuario = autenticar(chave)
                st.session_state["usuario"] = usuario
                banco.registrar_auditoria(usuario.perfil, "login_dashboard", "dashboard")
                st.rerun()
            except AcessoNegado as erro:
                banco.registrar_auditoria("desconhecido", "login_dashboard", "dashboard", "negado", {"motivo": str(erro)})
                st.error(str(erro))


@st.cache_data(ttl=30, show_spinner=False)
def dados_risco() -> pd.DataFrame:
    return relatorios.carregar_risco()


def pode(permissao: str) -> bool:
    return permissao in st.session_state["usuario"].permissoes


def rotulo_nivel(nivel: str) -> str:
    return f"{ICONE_NIVEL.get(nivel, '')} {nivel}"


# ==========================================
# ABAS
# ==========================================
def aba_visao_geral(df: pd.DataFrame, perfil: str):
    geral = relatorios.visao_geral(df)
    c = st.columns(5)
    c[0].metric("Equipamentos monitorados", geral["equipamentos_monitorados"])
    c[1].metric("Leituras processadas", f"{geral['leituras_processadas']:,}".replace(",", "."))
    c[2].metric("Alertas abertos", geral["alertas_abertos"], help=f"+ {geral['alertas_reconhecidos']} reconhecidos (inspeção agendada)")
    c[3].metric("Em nível CRÍTICO", geral["equipamentos_criticos"])
    c[4].metric("Score médio (7 dias)", geral["score_medio_7d"])

    atual = relatorios.estado_atual(df)
    esquerda, direita = st.columns([1, 2])
    with esquerda:
        dist = pd.DataFrame({"nivel": relatorios.ORDEM_NIVEIS,
                             "equipamentos": [geral["distribuicao_niveis_atual"][n] for n in relatorios.ORDEM_NIVEIS]})
        fig = px.bar(dist, x="nivel", y="equipamentos", color="nivel", color_discrete_map=COR_NIVEL, text="equipamentos",
                     title="Frota por nível de risco (situação atual)")
        fig.update_layout(**{**LAYOUT, "showlegend": False, "hovermode": "closest"})
        fig.update_xaxes(title=None)
        st.plotly_chart(fig, width="stretch")
    with direita:
        top = atual.head(15).iloc[::-1]
        fig = go.Figure(go.Bar(x=top["score_risco"], y=top["id_equipamento"], orientation="h",
                               marker_color=[COR_NIVEL[n] for n in top["nivel_risco"]],
                               text=[f"{s:.0f} · {n}" for s, n in zip(top["score_risco"], top["nivel_risco"])],
                               textposition="outside", customdata=top[["regiao", "tipo_equipamento", "tipo_operacao"]],
                               hovertemplate="%{y} · %{customdata[0]} · %{customdata[1]}<br>%{customdata[2]}<br>Score %{x:.1f}<extra></extra>"))
        fig.update_layout(**{**LAYOUT, "hovermode": "closest"}, title="Top 15 equipamentos por risco atual",
                          xaxis=dict(range=[0, 115], title="Score (probabilidade de falha em 7 dias, %)"))
        st.plotly_chart(fig, width="stretch")

    st.subheader("🚨 Alertas ativos e recomendação para o seu perfil")
    ativos = relatorios.alertas()
    ativos = ativos[ativos["id_equipamento"].isin(df["id_equipamento"].unique())]
    if ativos.empty:
        st.success("Nenhum alerta ativo para os filtros selecionados.")
        return
    chave_rec = perfil if perfil in ("operador", "tecnico", "gestor", "analista") else "gestor"
    ativos["nível"] = ativos["nivel_risco"].map(rotulo_nivel)
    ativos["recomendação"] = ativos["recomendacao"].map(lambda r: json.loads(r)[chave_rec])
    ativos["principais fatores"] = ativos["fatores_json"].map(
        lambda f: ", ".join(c["nome"] for c in json.loads(f)["principais_contribuicoes_modelo"]) or "—")
    st.dataframe(ativos[["id_alerta", "status", "id_equipamento", "nível", "score_risco", "regiao", "tipo_equipamento",
                         "tipo_operacao", "principais fatores", "recomendação", "aberto_em", "tratado_por", "observacao"]],
                 hide_index=True, width="stretch",
                 column_config={"score_risco": st.column_config.ProgressColumn("score", min_value=0, max_value=100, format="%.0f")})

    if pode("tratar_alertas"):
        with st.form("tratar_alerta"):
            st.markdown("**Dar baixa em um alerta** (fica registrado na auditoria)")
            c1, c2, c3 = st.columns([1, 1, 2])
            id_alerta = c1.selectbox("Alerta", ativos["id_alerta"],
                                     format_func=lambda i: f"#{i} · {ativos.set_index('id_alerta').loc[i, 'id_equipamento']}")
            novo_status = c2.selectbox("Novo status", ["RECONHECIDO", "RESOLVIDO"],
                                       help="RECONHECIDO = inspeção agendada · RESOLVIDO = manutenção concluída")
            observacao = c3.text_input("Observação", max_chars=500, placeholder="Ex.: rolamento trocado, OS 1234")
            if st.form_submit_button("Atualizar alerta"):
                try:
                    r = banco.tratar_alerta(int(id_alerta), novo_status, perfil, observacao or None)
                    banco.registrar_auditoria(perfil, "tratar_alerta", f"alerta {id_alerta}", "sucesso",
                                              {**r, "observacao": observacao, "origem": "dashboard"})
                    st.success(f"Alerta #{id_alerta}: {r['status_anterior']} → {r['status']}")
                    dados_risco.clear()
                except (banco.TransicaoInvalida, banco.AlertaNaoEncontrado) as erro:
                    st.error(str(erro))


def aba_tendencias(df: pd.DataFrame):
    rotulos = {"regiao": "Região", "tipo_operacao": "Tipo de operação", "tipo_equipamento": "Tipo de equipamento",
               "id_equipamento": "Equipamento"}
    c1, c2, c3 = st.columns([1, 1, 2])
    agrupar = c1.selectbox("Agrupar por", list(rotulos), format_func=rotulos.get)
    freq = c2.selectbox("Período", ["W-MON", "D"], format_func={"W-MON": "Semanal", "D": "Diário"}.get)
    opcoes = sorted(df[agrupar].unique())
    padrao = opcoes[:4] if agrupar == "id_equipamento" else opcoes
    selecionados = c3.multiselect(rotulos[agrupar], opcoes, default=list(relatorios.estado_atual(df).head(4)["id_equipamento"])
                                  if agrupar == "id_equipamento" else padrao)
    filtrado = df[df[agrupar].isin(selecionados)]
    if filtrado.empty:
        st.info("Selecione ao menos um item.")
        return
    serie = relatorios.tendencia(agrupar, freq, filtrado)
    ordem = [s for s in opcoes if s in selecionados]
    if len(ordem) > 8:
        st.caption("Mais de 8 séries: exibindo mapa de calor em vez de linhas.")
        tabela = serie.pivot(index=agrupar, columns="data_hora", values="score_medio")
        fig = px.imshow(tabela, aspect="auto", color_continuous_scale=["#cde2fb", "#6da7ec", "#2a78d6", "#1c5cab", "#0d366b"],
                        labels=dict(color="Score médio", x="", y=""), title=f"Score médio por {rotulos[agrupar].lower()}")
    else:
        fig = px.line(serie, x="data_hora", y="score_medio", color=agrupar, category_orders={agrupar: ordem},
                      color_discrete_sequence=CATEGORICA, title=f"Tendência do score médio por {rotulos[agrupar].lower()}",
                      labels={"data_hora": "", "score_medio": "Score médio", agrupar: rotulos[agrupar]})
        fig.update_traces(line_width=2)
    fig.update_layout(**LAYOUT)
    st.plotly_chart(fig, width="stretch")
    if agrupar != "id_equipamento":
        st.caption("Períodos com menos de 5 leituras no grupo são omitidos (amostra pequena demais para indicar tendência).")

    st.subheader(f"Resumo por {rotulos[agrupar].lower()}")
    resumo = relatorios.resumo_por(agrupar, filtrado)
    st.dataframe(resumo, hide_index=True, width="stretch", column_config={
        "score_medio": st.column_config.ProgressColumn("score médio", min_value=0, max_value=100, format="%.1f"),
        "pct_leituras_em_alerta": st.column_config.NumberColumn("% leituras em alerta", format="%.1f%%")})
    st.download_button("⬇️ Baixar resumo (CSV)", resumo.to_csv(index=False).encode("utf-8"),
                       f"resumo_{agrupar}.csv", "text/csv")


def aba_equipamento(df: pd.DataFrame, perfil: str):
    atual = relatorios.estado_atual(df)
    id_eq = st.selectbox("Equipamento", atual["id_equipamento"],
                         format_func=lambda e: f"{e} — {rotulo_nivel(atual.set_index('id_equipamento').loc[e, 'nivel_risco'])}")
    hist = df[df["id_equipamento"] == id_eq].sort_values("data_hora")
    ultima = hist.iloc[-1]
    c = st.columns(5)
    c[0].metric("Score atual", f"{ultima['score_risco']:.1f}")
    c[1].metric("Nível", rotulo_nivel(ultima["nivel_risco"]))
    c[2].metric("Tipo", ultima["tipo_equipamento"])
    c[3].metric("Região", ultima["regiao"])
    c[4].metric("Idade", f"{int(ultima['idade_anos'])} anos")

    fig = go.Figure()
    for (nome, minimo), (_, maximo) in zip(config.NIVEIS_RISCO, [(None, 100)] + config.NIVEIS_RISCO[:-1]):
        fig.add_hrect(y0=minimo, y1=maximo, fillcolor=COR_NIVEL[nome], opacity=0.08, line_width=0,
                      annotation_text=nome, annotation_position="top left", annotation_font_size=10)
    fig.add_trace(go.Scatter(x=hist["data_hora"], y=hist["score_risco"], mode="lines+markers", name="Score",
                             line=dict(color="#2a78d6", width=2), marker=dict(size=5)))
    fig.update_layout(**LAYOUT, title=f"Evolução do score — {id_eq}", yaxis=dict(range=[0, 100], title="Score"))
    st.plotly_chart(fig, width="stretch")

    sensores = {"vibracao_mm_s": "Vibração (mm/s)", "pressao_oleo_psi": "Pressão do óleo (psi)",
                "temperatura_motor_c": "Temperatura do motor (°C)"}
    cols = st.columns(3)
    for col, (campo, nome) in zip(cols, sensores.items()):
        f = px.line(hist, x="data_hora", y=campo, title=nome, labels={"data_hora": "", campo: ""},
                    color_discrete_sequence=["#52514e"])
        op, limite, *_ = config.LIMITES_OPERACIONAIS[campo]
        f.add_hline(y=limite, line_dash="dash", line_color=COR_NIVEL["ALTO"], annotation_text=f"limite {op} {limite}")
        f.update_layout(**{**LAYOUT, "height": 260})
        col.plotly_chart(f, width="stretch")

    fatores = json.loads(ultima["fatores_json"])
    rec = json.loads(ultima["recomendacao"])
    e, d = st.columns(2)
    with e:
        st.markdown("**Por que este score?** (última leitura)")
        for c_ in fatores["principais_contribuicoes_modelo"]:
            st.write(f"• **{c_['nome']}** = {c_['valor']:.1f} (típico saudável {c_['referencia_saudavel']}) → "
                     f"+{c_['contribuicao_pontos']} pontos")
        for v in fatores["limites_operacionais_violados"]:
            st.write(f"⚠️ {v['descricao']} (valor {v['valor']:.1f})")
        for r in fatores["regras_seguranca_acionadas"]:
            st.write(f"⛔ Regra de segurança: {r}")
        if not any(fatores.values()):
            st.write("Nenhum fator relevante: leitura dentro do padrão.")
    with d:
        st.markdown("**Recomendações por perfil**")
        for p_, texto in rec.items():
            destaque = "**" if p_ == perfil else ""
            st.write(f"{destaque}{TITULO_PERFIL.get(p_, p_)}:{destaque} {texto}")


def aba_nova_leitura(perfil: str):
    st.caption("Registra uma leitura manual (ex.: coletor sem conectividade). Passa pelas mesmas validações da API.")
    with st.form("nova_leitura"):
        c = st.columns(4)
        id_eq = c[0].text_input("Equipamento", "EQ-301")
        tipo = c[1].selectbox("Tipo", config.TIPOS_EQUIPAMENTO)
        regiao = c[2].selectbox("Região", config.REGIOES)
        operacao = c[3].selectbox("Operação", config.TIPOS_OPERACAO, help="Colheitadeira: Colheita/Transporte · Pulverizador: Pulverização/Transporte")
        c = st.columns(5)
        idade = c[0].number_input("Idade (anos)", 0, 40, 8)
        horas = c[1].number_input("Horas de uso contínuo", 0.0, 24.0, 9.0)
        rpm = c[2].number_input("RPM médio", 0, 3500, 1900)
        temp = c[3].number_input("Temp. motor (°C)", -10.0, 150.0, 90.0)
        pressao = c[4].number_input("Pressão óleo (psi)", 0.0, 100.0, 42.0)
        c = st.columns(5)
        vib = c[0].number_input("Vibração (mm/s)", 0.0, 50.0, 4.5)
        carga = c[1].number_input("Carga (%)", 0.0, 100.0, 65.0)
        amb = c[2].number_input("Temp. ambiente (°C)", -10.0, 55.0, 28.0)
        umid = c[3].number_input("Umidade (%)", 0.0, 100.0, 55.0)
        decl = c[4].number_input("Declividade (%)", 0.0, 60.0, 6.0)
        enviar = st.form_submit_button("Calcular risco e registrar", type="primary")
    if not enviar:
        return
    entrada = {"id_equipamento": id_eq, "tipo_equipamento": tipo, "regiao": regiao, "tipo_operacao": operacao,
               "idade_anos": idade, "horas_uso_continuo": horas, "rpm_medio": rpm, "temperatura_motor_c": temp,
               "pressao_oleo_psi": pressao, "vibracao_mm_s": vib, "carga_motor_pct": carga,
               "temperatura_ambiente_c": amb, "umidade_relativa_pct": umid, "declividade_terreno_pct": decl,
               "data_hora": datetime.now().replace(microsecond=0).isoformat()}
    try:
        r = servico.processar_leitura(entrada, perfil, origem="dashboard")
    except ValidationError as erro:
        st.error("Leitura rejeitada: " + "; ".join(e["msg"] for e in erro.errors()))
        return
    except (banco.LeituraDuplicada, modelo.ModeloIndisponivel) as erro:
        st.error(str(erro))
        return
    dados_risco.clear()
    caixa = {"CRÍTICO": st.error, "ALTO": st.warning, "MODERADO": st.info, "BAIXO": st.success}[r["nivel_risco"]]
    caixa(f"{rotulo_nivel(r['nivel_risco'])} — score {r['score_risco']:.1f} (probabilidade de falha em 7 dias)")
    chave_rec = perfil if perfil in r["recomendacoes"] else "gestor"
    st.write("**Recomendação:**", r["recomendacoes"][chave_rec])
    st.json({"fatores": r["fatores"], "id_leitura": r["id_leitura"], "versao_modelo": r["versao_modelo"]}, expanded=False)


def aba_modelo():
    m = modelo.carregar_metricas()
    t = m["teste"]
    st.markdown(f"**Versão:** `{m['versao_modelo']}` · **Algoritmo:** {m['algoritmo_escolhido']} · "
                f"**Teste a partir de:** {m['periodo_teste_a_partir_de']} · **Critério de escolha:** {m['criterio_escolha']}")
    c = st.columns(5)
    c[0].metric("PR-AUC", t["pr_auc"])
    c[1].metric("ROC-AUC", t["roc_auc"])
    c[2].metric("Recall (nível ALTO)", f"{t['recall_alerta']:.0%}")
    c[3].metric("Precisão (nível ALTO)", f"{t['precisao_alerta']:.0%}")
    c[4].metric("Brier", t["brier"])
    comp = pd.DataFrame([{"modelo": k, "PR-AUC validação": v["validacao"]["pr_auc"], "PR-AUC teste": v["teste"]["pr_auc"],
                          "ROC-AUC teste": v["teste"]["roc_auc"], "Recall ALTO": v["teste"]["recall_alerta"],
                          "Precisão ALTO": v["teste"]["precisao_alerta"]} for k, v in m["comparacao_modelos"].items()])
    st.dataframe(comp, hide_index=True, width="stretch")
    e, d = st.columns(2)
    imp = pd.DataFrame(m["importancia_variaveis"]).head(10).iloc[::-1]
    fig = px.bar(imp, x="queda_pr_auc", y="variavel", orientation="h", title="Importância por permutação (queda na PR-AUC)",
                 color_discrete_sequence=["#2a78d6"], labels={"queda_pr_auc": "", "variavel": ""})
    fig.update_layout(**{**LAYOUT, "hovermode": "closest"})
    e.plotly_chart(fig, width="stretch")
    niveis = pd.DataFrame([{"corte": k, "recall": v["recall_alerta"], "precisão": v["precisao_alerta"],
                            **v["matriz_confusao"]} for k, v in m["desempenho_por_nivel"].items()])
    d.markdown("**Desempenho em cada corte de nível (período de teste)**")
    d.dataframe(niveis, hide_index=True, width="stretch")
    d.markdown("**Critérios:** " + " · ".join(f"{n} ≥ {v}" for n, v in config.NIVEIS_RISCO) +
               "  \n**Regras de segurança (forçam CRÍTICO):** " +
               " · ".join(desc for _, _, desc in config.REGRAS_SEGURANCA.values()))


def aba_auditoria(perfil: str):
    if st.button("🔒 Verificar integridade (hashes)"):
        leituras = banco.verificar_integridade_leituras()
        trilha = banco.verificar_integridade_auditoria()
        banco.registrar_auditoria(perfil, "verificar_integridade", "dashboard", "sucesso",
                                  {"leituras": leituras["integra"], "auditoria": trilha["integra"]})
        (st.success if leituras["integra"] else st.error)(
            f"Leituras: {'íntegras' if leituras['integra'] else 'VIOLADAS'} ({leituras['leituras_verificadas']} verificadas)")
        (st.success if trilha["integra"] else st.error)(
            f"Trilha de auditoria: {'íntegra' if trilha['integra'] else 'VIOLADA no evento ' + str(trilha['primeiro_evento_violado'])}"
            f" ({trilha['eventos_verificados']} eventos)")
    trilha = banco.consultar("SELECT id_evento, data_hora, perfil, acao, recurso, resultado, detalhes_json, hash_evento "
                             "FROM auditoria ORDER BY id_evento DESC LIMIT 300")
    st.dataframe(trilha, hide_index=True, width="stretch")


# ==========================================
# APLICAÇÃO
# ==========================================
if "usuario" not in st.session_state:
    tela_login()
    st.stop()

usuario = st.session_state["usuario"]
with st.sidebar:
    st.markdown(f"### 👤 {TITULO_PERFIL[usuario.perfil]}")
    st.caption("Permissões: " + ", ".join(sorted(usuario.permissoes)))
    if st.button("Atualizar dados"):
        dados_risco.clear()
    if st.button("Sair"):
        banco.registrar_auditoria(usuario.perfil, "logout_dashboard", "dashboard")
        del st.session_state["usuario"]
        st.rerun()

st.title("🚜 Prevenção de Quebra de Maquinário — Sompo Seguros")
try:
    df = dados_risco()
except Exception:  # banco ainda não criado
    df = pd.DataFrame()
if df.empty:
    st.warning("Ainda não há dados pontuados. Execute `python main.py pipeline` e recarregue a página.")
    st.stop()

with st.sidebar:
    st.divider()
    st.markdown("**Filtros**")
    regioes = st.multiselect("Região", config.REGIOES, default=config.REGIOES)
    operacoes = st.multiselect("Tipo de operação", config.TIPOS_OPERACAO, default=config.TIPOS_OPERACAO)
    inicio, fim = df["data_hora"].min().date(), df["data_hora"].max().date()
    periodo = st.date_input("Período", (inicio, fim), min_value=inicio, max_value=fim)
df_filtrado = df[df["regiao"].isin(regioes) & df["tipo_operacao"].isin(operacoes)]
if isinstance(periodo, tuple) and len(periodo) == 2:
    df_filtrado = df_filtrado[(df_filtrado["data_hora"].dt.date >= periodo[0]) & (df_filtrado["data_hora"].dt.date <= periodo[1])]
if df_filtrado.empty:
    st.info("Nenhum dado para os filtros selecionados.")
    st.stop()

abas = [("📊 Visão geral", "ver_alertas", lambda: aba_visao_geral(df_filtrado, usuario.perfil)),
        ("📈 Tendências", "ver_relatorios", lambda: aba_tendencias(df_filtrado)),
        ("🔧 Equipamento", "ver_alertas", lambda: aba_equipamento(df_filtrado, usuario.perfil)),
        ("➕ Nova leitura", "enviar_telemetria", lambda: aba_nova_leitura(usuario.perfil)),
        ("🧠 Modelo", "ver_modelo", aba_modelo),
        ("🛡️ Auditoria", "ver_auditoria", lambda: aba_auditoria(usuario.perfil))]
permitidas = [(nome, fn) for nome, perm, fn in abas if pode(perm)]
for aba, (_, fn) in zip(st.tabs([n for n, _ in permitidas]), permitidas):
    with aba:
        fn()
