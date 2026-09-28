"""Validação da integração com as fontes de dados e confiabilidade da coleta.

Simula sensores em campo enviando leituras pela API (individual, em lote e em
paralelo) e verifica que NADA se perde ou é corrompido no caminho até o banco.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np
from streamlit.testing.v1 import AppTest
import pytest

from conftest import CHAVES, cabecalho
from sompo import banco, config, relatorios
from sompo.seguranca import autenticar, hash_conteudo
from sompo.simulador import gerar_leitura_ao_vivo


def _leituras_simuladas(n: int, prefixo: int, semente: int) -> list[dict]:
    rng = np.random.default_rng(semente)
    base = datetime(2026, 9, 10, 6, 0, 0)
    leituras = []
    for i in range(n):
        eq = i % 20  # 20 equipamentos, cada um com tipo/região/idade fixos
        leitura = gerar_leitura_ao_vivo(rng, f"EQ-{prefixo + eq}", config.TIPOS_EQUIPAMENTO[eq % 3],
                                        config.REGIOES[eq % 8], 1 + eq % 18,
                                        estressada=(i % 7 == 0))
        leitura["data_hora"] = (base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        leituras.append(leitura)
    return leituras


def _conferir_sem_perda_nem_corrupcao(enviadas: list[dict], ids: list[int]):
    assert len(ids) == len(set(ids)) == len(enviadas)
    gravadas = banco.consultar(f"SELECT * FROM leituras WHERE id_leitura IN ({','.join('?' * len(ids))})", tuple(ids))
    assert len(gravadas) == len(enviadas)
    por_chave = {(g["id_equipamento"], g["data_hora"]): g for g in gravadas.to_dict("records")}
    for original in enviadas:
        gravada = por_chave[(original["id_equipamento"], original["data_hora"])]
        # O hash calculado sobre o que o SENSOR enviou bate com o que está no banco
        assert hash_conteudo(banco.conteudo_leitura(original)) == gravada["hash_leitura"]
    predicoes = banco.consultar(f"SELECT COUNT(*) AS n FROM predicoes WHERE id_leitura IN ({','.join('?' * len(ids))})", tuple(ids))
    assert predicoes["n"][0] == len(enviadas)


def test_carga_historica_sem_perda(resumo_pipeline):
    carga = resumo_pipeline["carga"]
    assert carga["sem_perda"] and carga["integridade_hash"]
    assert resumo_pipeline["leituras_pontuadas"] == carga["no_banco"]
    q = resumo_pipeline["qualidade_dados"]
    assert q["taxa_aproveitamento_pct"] > 95


def test_coleta_individual_sem_perda_nem_corrupcao(cliente):
    enviadas = _leituras_simuladas(60, 400, semente=1)
    auditoria_antes = banco.consultar("SELECT COUNT(*) AS n FROM auditoria")["n"][0]
    ids = []
    for leitura in enviadas:
        r = cliente.post("/telemetria", json=leitura, headers=cabecalho("operador"))
        assert r.status_code == 200, r.text
        ids.append(r.json()["id_leitura"])
    _conferir_sem_perda_nem_corrupcao(enviadas, ids)
    auditoria_depois = banco.consultar("SELECT COUNT(*) AS n FROM auditoria")["n"][0]
    assert auditoria_depois - auditoria_antes == len(enviadas)  # uma entrada de auditoria por leitura


def test_coleta_em_lote_sem_perda_nem_corrupcao(cliente):
    enviadas = _leituras_simuladas(200, 500, semente=2)
    r = cliente.post("/telemetria/lote", json=enviadas, headers=cabecalho("tecnico"))
    assert r.status_code == 200 and r.json()["aceitas"] == 200
    _conferir_sem_perda_nem_corrupcao(enviadas, [x["id_leitura"] for x in r.json()["resultados"]])


def test_coleta_concorrente_mantem_integridade(cliente):
    enviadas = _leituras_simuladas(80, 600, semente=3)

    def enviar(leitura):
        return cliente.post("/telemetria", json=leitura, headers=cabecalho("operador"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        respostas = list(executor.map(enviar, enviadas))
    assert all(r.status_code == 200 for r in respostas)
    _conferir_sem_perda_nem_corrupcao(enviadas, [r.json()["id_leitura"] for r in respostas])
    assert banco.verificar_integridade_auditoria()["integra"]  # corrente de hashes intacta mesmo em paralelo


def test_equipamentos_em_degradacao_recebem_score_maior(cliente):
    rng = np.random.default_rng(4)
    normais, degradadas = [], []
    for i in range(15):
        for estressada, destino in ((False, normais), (True, degradadas)):
            leitura = gerar_leitura_ao_vivo(rng, f"EQ-{700 + i + (50 if estressada else 0)}", "Trator", "GO", 10, estressada)
            leitura["data_hora"] = f"2026-09-11 {8 + i % 10:02d}:{i:02d}:00"
            destino.append(cliente.post("/telemetria", json=leitura, headers=cabecalho("tecnico")).json()["score_risco"])
    assert np.mean(degradadas) > np.mean(normais) + 30


def test_relatorio_html_gerado(ambiente):
    caminho = relatorios.gerar_relatorio(salvar_evidencias=False)
    html = open(caminho, encoding="utf-8").read()
    assert "Relatório de Risco da Frota" in html and "data:image/png;base64" in html


@pytest.mark.parametrize("perfil", list(CHAVES))
def test_dashboard_abre_sem_erros_para_cada_perfil(ambiente, perfil):
    app = AppTest.from_file(str(config.RAIZ / "dashboard" / "app.py"), default_timeout=180)
    app.session_state["usuario"] = autenticar(CHAVES[perfil])
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    abas = [t.label for t in app.tabs]
    assert ("🛡️ Auditoria" in abas) == (perfil in ("analista", "admin"))
    assert ("➕ Nova leitura" in abas) == (perfil in ("operador", "tecnico", "admin"))
