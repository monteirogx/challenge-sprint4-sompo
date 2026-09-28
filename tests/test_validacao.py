"""Contrato de dados e pipeline de limpeza."""
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from conftest import leitura_exemplo
from sompo import config
from sompo.simulador import injetar_inconsistencias, simular_frota
from sompo.validacao import LeituraTelemetria, limpar_lote, normalizar_operacao, normalizar_regiao


@pytest.mark.parametrize("entrada,esperado", [
    ("MT", "MT"), (" mt ", "MT"), ("Mato Grosso", "MT"), ("Paraná", "PR"), ("parana", "PR"), ("XX", None), (None, None)])
def test_normaliza_regiao(entrada, esperado):
    assert normalizar_regiao(entrada) == esperado


def test_normaliza_operacao_sem_acento():
    assert normalizar_operacao("pulverizacao") == "Pulverização"
    assert normalizar_operacao("COLHEITA ") == "Colheita"


def test_leitura_valida_e_normalizada():
    leitura = LeituraTelemetria.model_validate(leitura_exemplo(id_eq=" eq-010", regiao="parana"))
    assert leitura.id_equipamento == "EQ-010"
    assert leitura.regiao == "PR"
    assert leitura.campos_ausentes() == []


def test_campos_ausentes_sao_aceitos_ate_o_limite():
    leitura = LeituraTelemetria.model_validate(leitura_exemplo(pressao_oleo_psi=None, umidade_relativa_pct=None))
    assert set(leitura.campos_ausentes()) == {"pressao_oleo_psi", "umidade_relativa_pct"}


@pytest.mark.parametrize("alteracao,trecho_erro", [
    ({"temperatura_motor_c": -999}, "temperatura_motor_c"),
    ({"rpm_medio": 9000}, "rpm_medio"),
    ({"regiao": "XX"}, "região inválida"),
    ({"id_equipamento": "trator-1"}, "EQ-000"),
    ({"tipo_equipamento": "Colheitadeira", "tipo_operacao": "Plantio"}, "não realiza"),
    ({"temperatura_motor_c": None, "pressao_oleo_psi": None, "vibracao_mm_s": None, "rpm_medio": None}, "sensores ausentes"),
    ({"campo_desconhecido": 1}, "campo_desconhecido"),
    ({"data_hora": "2099-01-01 00:00:00"}, "futuro"),
])
def test_leituras_invalidas_sao_rejeitadas(alteracao, trecho_erro):
    with pytest.raises(ValidationError) as erro:
        LeituraTelemetria.model_validate(leitura_exemplo(**alteracao))
    assert trecho_erro in str(erro.value)


def test_limpeza_trata_todas_as_inconsistencias():
    limpo = simular_frota(n_equipamentos=10, dias=40)
    sujo = injetar_inconsistencias(limpo)
    tratado, rel = limpar_lote(sujo)

    # Contabilidade fecha: tudo que entrou foi aproveitado ou descartado com motivo
    descartes = rel["descartadas_identificacao_invalida"] + rel["descartadas_duplicadas"] + rel["descartadas_excesso_ausentes"]
    assert rel["linhas_validas"] + descartes == rel["linhas_recebidas"]
    # Resultado final sem duplicidades, sem ausentes, dentro das faixas e com textos padronizados
    assert not tratado.duplicated(["id_equipamento", "data_hora"]).any()
    assert not tratado[config.FEATURES_NUMERICAS].isna().any().any()
    for col, (minimo, maximo) in config.FAIXAS_VALIDAS.items():
        assert tratado[col].between(minimo, maximo).all(), col
    assert set(tratado["regiao"]) <= set(config.REGIOES)
    assert set(tratado["tipo_operacao"]) <= set(config.TIPOS_OPERACAO)
    assert tratado["id_equipamento"].str.fullmatch(r"EQ-\d{3}").all()
    assert rel["valores_fora_da_faixa_anulados"] > 0 and rel["valores_imputados"] > 0


def test_limpeza_nao_altera_dados_que_ja_estavam_corretos():
    limpo = simular_frota(n_equipamentos=5, dias=20)
    tratado, rel = limpar_lote(limpo)
    assert rel["linhas_validas"] == len(limpo)
    original = limpo.sort_values(["id_equipamento", "data_hora"]).reset_index(drop=True)
    assert np.allclose(tratado["temperatura_motor_c"], original["temperatura_motor_c"])


def test_lote_sem_coluna_obrigatoria_gera_erro_claro():
    with pytest.raises(ValueError, match="Colunas obrigatórias ausentes"):
        limpar_lote(pd.DataFrame({"id_equipamento": ["EQ-001"]}))
