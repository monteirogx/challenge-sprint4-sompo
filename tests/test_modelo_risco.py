"""Qualidade do modelo, reprodutibilidade, integridade e regras de risco."""
import pandas as pd
import pytest

from conftest import leitura_critica, leitura_exemplo
from sompo import banco, config, modelo, risco
from sompo.simulador import simular_frota


@pytest.fixture(scope="module")
def metricas(ambiente):
    return modelo.carregar_metricas()


def test_modelo_final_supera_o_baseline_da_sprint2(metricas):
    comp = metricas["comparacao_modelos"]
    escolhido = metricas["algoritmo_escolhido"]
    assert comp[escolhido]["teste"]["pr_auc"] > comp["baseline_sprint2_random_forest"]["teste"]["pr_auc"] + 0.2


def test_desempenho_minimo_no_periodo_de_teste(metricas):
    t = metricas["teste"]
    assert t["roc_auc"] >= 0.85
    assert t["pr_auc"] >= 3 * t["taxa_base_falha"]  # muito melhor que o acaso
    assert t["recall_alerta"] >= 0.5


def test_divisao_temporal_sem_vazamento(ambiente):
    df = banco.carregar_dataset_rotulado()
    treino, teste, corte = modelo.dividir_temporal(df)
    assert pd.to_datetime(treino["data_hora"]).max() < pd.Timestamp(corte) <= pd.to_datetime(teste["data_hora"]).min()


def test_janela_censurada_nao_entra_no_treino():
    df = simular_frota(n_equipamentos=5, dias=30)
    datas = pd.to_datetime(df["data_hora"])
    ultimos = datas >= datas.min().normalize() + pd.Timedelta(days=30 - config.HORIZONTE_DIAS)
    # Nos últimos 7 dias o futuro é desconhecido: só pode haver 1 (falha já observada) ou vazio, nunca 0
    assert ultimos.any() and not (df.loc[ultimos, config.ALVO] == 0).any()
    assert df.loc[~ultimos, config.ALVO].notna().all()


def test_simulacao_reproduzivel():
    a, b = simular_frota(5, 20), simular_frota(5, 20)
    pd.testing.assert_frame_equal(a, b)


def test_modelo_adulterado_nao_e_carregado(ambiente):
    caminho = config.caminho_modelo()
    original = caminho.read_bytes()
    try:
        caminho.write_bytes(original + b"adulterado")
        modelo._cache.clear()
        with pytest.raises(modelo.ModeloIndisponivel, match="adulterado"):
            modelo.carregar()
    finally:
        caminho.write_bytes(original)
        modelo._cache.clear()
    assert modelo.carregar()["versao"]


def test_leitura_degradada_tem_score_maior_e_explicacao(ambiente):
    pacote = modelo.carregar()
    df = pd.DataFrame([leitura_exemplo(), leitura_critica()])
    prob = modelo.prever(pacote, df)
    assert prob[1] > prob[0]
    explicacoes = modelo.explicar(pacote, df, prob)
    variaveis = {e["variavel"] for e in explicacoes[1]}
    assert variaveis & {"vibracao_mm_s", "pressao_oleo_psi", "temperatura_motor_c"}


@pytest.mark.parametrize("score,nivel", [(0, "BAIXO"), (9.9, "BAIXO"), (10, "MODERADO"), (24.9, "MODERADO"),
                                         (25, "ALTO"), (59.9, "ALTO"), (60, "CRÍTICO"), (100, "CRÍTICO")])
def test_cortes_dos_niveis(score, nivel):
    assert risco.classificar_nivel(score) == nivel


def test_regra_de_seguranca_forca_critico_mesmo_com_modelo_baixo():
    avaliacao = risco.avaliar(leitura_exemplo(temperatura_motor_c=112), probabilidade=0.02, contribuicoes=[])
    assert avaliacao["nivel_risco"] == "CRÍTICO" and avaliacao["alerta"]
    assert "Superaquecimento" in avaliacao["regra_seguranca"]


def test_recomendacoes_para_todos_os_perfis():
    for prob in (0.01, 0.15, 0.4, 0.9):
        rec = risco.avaliar(leitura_critica(), prob, [])["recomendacoes"]
        assert set(rec) == {"operador", "tecnico", "gestor", "analista"} and all(rec.values())


def test_limites_violados_apontam_componente():
    violados = risco.limites_violados(leitura_critica())
    assert any(v["variavel"] == "vibracao_mm_s" and "rolamentos" in v["componente"] for v in violados)
