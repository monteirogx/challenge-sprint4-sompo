"""API REST: autenticação, autorização, validação e fluxo de ponta a ponta."""
import pytest

from conftest import cabecalho, leitura_critica, leitura_exemplo
from sompo import banco


def test_saude_sem_autenticacao(cliente):
    r = cliente.get("/saude")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["versao_modelo"]


@pytest.mark.parametrize("cabecalhos", [{}, {"X-API-Key": "errada"}])
def test_sem_chave_valida_retorna_401(cliente, cabecalhos):
    r = cliente.post("/telemetria", json=leitura_exemplo("EQ-700"), headers=cabecalhos)
    assert r.status_code == 401 and r.json()["erro"] == "nao_autenticado"


@pytest.mark.parametrize("perfil,metodo,rota,esperado", [
    ("gestor", "POST", "/telemetria", 403), ("analista", "POST", "/telemetria", 403),
    ("operador", "GET", "/auditoria", 403), ("operador", "GET", "/relatorios/visao-geral", 403),
    ("tecnico", "GET", "/modelo", 403), ("gestor", "GET", "/auditoria/integridade", 403),
    ("gestor", "GET", "/relatorios/visao-geral", 200), ("analista", "GET", "/modelo", 200),
    ("analista", "GET", "/auditoria", 200), ("operador", "GET", "/alertas", 200),
])
def test_controle_de_acesso_por_perfil(cliente, perfil, metodo, rota, esperado):
    corpo = leitura_exemplo("EQ-701", "2026-09-02 10:00:00") if metodo == "POST" else None
    r = cliente.request(metodo, rota, json=corpo, headers=cabecalho(perfil))
    assert r.status_code == esperado


def test_fluxo_completo_de_uma_leitura(cliente):
    r = cliente.post("/telemetria", json=dict(leitura_critica("EQ-710"), operador_id="MAT-555"), headers=cabecalho("operador"))
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["nivel_risco"] in ("ALTO", "CRÍTICO") and corpo["alerta"]
    assert corpo["fatores"]["principais_contribuicoes_modelo"]
    assert set(corpo["recomendacoes"]) == {"operador", "tecnico", "gestor", "analista"}
    assert "X-Request-ID" in r.headers

    # Rastreabilidade: leitura, predição e evento de auditoria gravados e ligados entre si
    leitura = banco.consultar("SELECT * FROM leituras WHERE id_leitura = ?", (corpo["id_leitura"],)).iloc[0]
    assert leitura["origem"] == "api" and leitura["operador_pseudonimo"].startswith("OP-")
    assert "MAT-555" not in str(leitura.to_dict())  # matrícula nunca é gravada em claro
    predicao = banco.consultar("SELECT * FROM predicoes WHERE id_leitura = ?", (corpo["id_leitura"],)).iloc[0]
    assert predicao["score_risco"] == corpo["score_risco"] and predicao["versao_modelo"] == corpo["versao_modelo"]
    auditoria = banco.consultar("SELECT * FROM auditoria WHERE acao = 'enviar_telemetria' AND recurso = 'EQ-710'")
    assert f'"id_leitura": {corpo["id_leitura"]}' in auditoria.iloc[-1]["detalhes_json"]

    # O alerta aparece para o gestor
    alertas = cliente.get("/alertas", headers=cabecalho("gestor")).json()
    assert "EQ-710" in {a["id_equipamento"] for a in alertas["alertas"]}


def test_leitura_invalida_retorna_422_detalhado_e_e_auditada(cliente):
    r = cliente.post("/telemetria", json=leitura_exemplo("EQ-720", temperatura_motor_c=-999, regiao="ZZ"),
                     headers=cabecalho("operador"))
    assert r.status_code == 422
    campos = {d["campo"] for d in r.json()["detalhe"]}
    assert {"temperatura_motor_c", "regiao"} <= campos
    auditoria = banco.consultar("SELECT * FROM auditoria WHERE recurso = 'EQ-720'")
    assert auditoria.iloc[-1]["resultado"] == "rejeitada"


def test_reenvio_duplicado_retorna_409(cliente):
    leitura = leitura_exemplo("EQ-730", "2026-09-03 07:00:00")
    assert cliente.post("/telemetria", json=leitura, headers=cabecalho("operador")).status_code == 200
    assert cliente.post("/telemetria", json=leitura, headers=cabecalho("operador")).status_code == 409


def test_sensores_ausentes_sao_completados_e_sinalizados(cliente):
    leitura = leitura_exemplo("EQ-740", "2026-09-03 08:00:00", pressao_oleo_psi=None, vibracao_mm_s=None)
    r = cliente.post("/telemetria", json=leitura, headers=cabecalho("tecnico"))
    assert r.status_code == 200
    assert set(r.json()["campos_imputados"]) == {"pressao_oleo_psi", "vibracao_mm_s"}
    gravada = banco.consultar("SELECT * FROM leituras WHERE id_leitura = ?", (r.json()["id_leitura"],)).iloc[0]
    assert gravada["qtd_campos_imputados"] == 2 and gravada["pressao_oleo_psi"] > 0


def test_lote_com_itens_invalidos_nao_interrompe_os_validos(cliente):
    lote = [leitura_exemplo(f"EQ-75{i}", "2026-09-03 09:00:00") for i in range(3)]
    lote += [{"id_equipamento": "abc"}, leitura_exemplo("EQ-760", temperatura_motor_c=999)]
    r = cliente.post("/telemetria/lote", json=lote, headers=cabecalho("tecnico"))
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["aceitas"] == 3 and corpo["rejeitadas"] == 2
    assert [x["status"] for x in corpo["resultados"]] == ["aceita"] * 3 + ["rejeitada"] * 2


@pytest.mark.parametrize("agrupar_por", ["regiao", "tipo_operacao", "tipo_equipamento", "id_equipamento"])
def test_relatorios_por_agrupamento(cliente, agrupar_por):
    resumo = cliente.get(f"/relatorios/resumo?agrupar_por={agrupar_por}", headers=cabecalho("gestor")).json()
    assert resumo and all({agrupar_por, "score_medio", "pct_leituras_em_alerta"} <= set(l) for l in resumo)
    tendencia = cliente.get(f"/relatorios/tendencia?agrupar_por={agrupar_por}", headers=cabecalho("gestor")).json()
    assert tendencia and "score_medio" in tendencia[0]


def test_agrupamento_invalido_e_recusado(cliente):
    assert cliente.get("/relatorios/resumo?agrupar_por=senha", headers=cabecalho("gestor")).status_code == 422


def test_integridade_via_api(cliente):
    r = cliente.get("/auditoria/integridade", headers=cabecalho("analista")).json()
    assert r["auditoria"]["integra"] and r["leituras"]["integra"]


def test_modelo_indisponivel_retorna_503_sem_derrubar_a_api(cliente, monkeypatch, tmp_path):
    from sompo import modelo
    monkeypatch.setenv("SOMPO_MODELO_PATH", str(tmp_path / "nao_existe.joblib"))
    modelo._cache.clear()
    r = cliente.post("/telemetria", json=leitura_exemplo("EQ-770", "2026-09-04 10:00:00"), headers=cabecalho("operador"))
    assert r.status_code == 503 and r.json()["erro"] == "modelo_indisponivel"
    assert cliente.get("/saude").json()["status"] == "degradado"
    monkeypatch.undo()
    modelo._cache.clear()
    assert cliente.get("/saude").json()["status"] == "ok"


# ==========================================
# ALERTAS: tabela própria e ciclo de vida
# ==========================================
def _enviar_critica(cliente, id_eq, hora):
    r = cliente.post("/telemetria", json=leitura_critica(id_eq, f"2026-09-05 {hora}:00:00"), headers=cabecalho("operador"))
    assert r.status_code == 200, r.text
    return r.json()


def test_pipeline_abre_alertas_para_equipamentos_em_risco(resumo_pipeline):
    from sompo import relatorios
    # Só os equipamentos da carga histórica (outros testes criam alertas via API no mesmo banco)
    atual = relatorios.estado_atual()
    historicos = atual[atual["origem"] == "historico"]
    em_alerta = set(historicos.loc[historicos["alerta"], "id_equipamento"])
    assert resumo_pipeline["alertas_abertos"] == len(em_alerta) > 0
    com_alerta = set(banco.consultar("SELECT id_equipamento FROM alertas")["id_equipamento"])
    assert em_alerta <= com_alerta


def test_ciclo_de_vida_do_alerta(cliente):
    primeira = _enviar_critica(cliente, "EQ-780", "08")
    assert primeira["acao_alerta"] == "criado" and primeira["id_alerta"]
    id_alerta = primeira["id_alerta"]

    # Nova leitura crítica do mesmo equipamento atualiza o MESMO alerta (sem duplicar)
    segunda = _enviar_critica(cliente, "EQ-780", "09")
    assert segunda["id_alerta"] == id_alerta and segunda["acao_alerta"] == "atualizado"
    ativos = banco.consultar("SELECT * FROM alertas WHERE id_equipamento = 'EQ-780' AND status <> 'RESOLVIDO'")
    assert len(ativos) == 1 and ativos.iloc[0]["id_predicao"] == segunda["id_predicao"]

    # Operador não trata alertas; técnico reconhece; gestor resolve
    corpo = {"status": "RECONHECIDO", "observacao": "Inspeção agendada"}
    assert cliente.patch(f"/alertas/{id_alerta}", json=corpo, headers=cabecalho("operador")).status_code == 403
    r = cliente.patch(f"/alertas/{id_alerta}", json=corpo, headers=cabecalho("tecnico"))
    assert r.status_code == 200 and r.json()["status_anterior"] == "ABERTO"
    r = cliente.patch(f"/alertas/{id_alerta}", json={"status": "RESOLVIDO", "observacao": "Rolamento trocado"},
                      headers=cabecalho("gestor"))
    assert r.status_code == 200 and r.json()["status"] == "RESOLVIDO"

    # Resolvido sai da lista de ativos, aparece no filtro de resolvidos e não pode ser reaberto
    ativos_api = cliente.get("/alertas", headers=cabecalho("gestor")).json()["alertas"]
    assert id_alerta not in {a["id_alerta"] for a in ativos_api}
    resolvidos = cliente.get("/alertas?status=RESOLVIDO", headers=cabecalho("gestor")).json()["alertas"]
    alerta = next(a for a in resolvidos if a["id_alerta"] == id_alerta)
    assert alerta["tratado_por"] == "gestor" and alerta["observacao"] == "Rolamento trocado"
    r = cliente.patch(f"/alertas/{id_alerta}", json={"status": "RECONHECIDO"}, headers=cabecalho("gestor"))
    assert r.status_code == 409

    # Depois de resolvido, uma nova leitura crítica abre um alerta NOVO
    terceira = _enviar_critica(cliente, "EQ-780", "10")
    assert terceira["acao_alerta"] == "criado" and terceira["id_alerta"] != id_alerta

    # Toda mudança de status ficou na auditoria
    trilha = banco.consultar("SELECT * FROM auditoria WHERE acao = 'tratar_alerta' AND recurso = ?", (f"alerta {id_alerta}",))
    assert list(trilha["perfil"]) == ["tecnico", "gestor"]


def test_alerta_reconhecido_volta_a_aberto_se_o_risco_escalar():
    from sompo import banco as b
    id_predicao = int(b.consultar("SELECT MAX(id_predicao) AS id FROM predicoes")["id"][0])
    b.inserir_leitura(leitura_exemplo("EQ-790", "2026-09-06 08:00:00"), "teste")
    id_alerta, acao = b.registrar_alerta("EQ-790", id_predicao, "ALTO", 40.0)
    assert acao == "criado"
    b.tratar_alerta(id_alerta, "RECONHECIDO", "tecnico")
    assert b.registrar_alerta("EQ-790", id_predicao, "ALTO", 45.0) == (id_alerta, "atualizado")
    assert b.registrar_alerta("EQ-790", id_predicao, "CRÍTICO", 85.0) == (id_alerta, "reescalado")
    status = b.consultar("SELECT status FROM alertas WHERE id_alerta = ?", (id_alerta,))["status"][0]
    assert status == "ABERTO"


def test_alerta_inexistente_retorna_404(cliente):
    r = cliente.patch("/alertas/999999", json={"status": "RESOLVIDO"}, headers=cabecalho("gestor"))
    assert r.status_code == 404
