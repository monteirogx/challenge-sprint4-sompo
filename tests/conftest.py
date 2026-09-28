"""Ambiente de teste isolado: banco, modelo, chaves e pastas temporários.

O pipeline completo roda uma vez por sessão com uma frota menor, e todos os
testes usam esse ambiente, sem tocar nos dados reais do projeto.
"""
import pytest

CHAVES = {"operador": "chave-teste-operador", "tecnico": "chave-teste-tecnico", "gestor": "chave-teste-gestor",
          "analista": "chave-teste-analista", "admin": "chave-teste-admin"}


@pytest.fixture(scope="session", autouse=True)
def ambiente(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("sompo")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SOMPO_DB_PATH", str(pasta / "teste.db"))
        mp.setenv("SOMPO_MODELO_PATH", str(pasta / "modelo" / "modelo_teste.joblib"))
        mp.setenv("SOMPO_SEGREDO_PSEUDONIMO", "segredo-de-teste")
        for perfil, chave in CHAVES.items():
            mp.setenv(f"SOMPO_API_KEY_{perfil.upper()}", chave)

        from sompo import config
        for nome in ("DIR_DADOS_BRUTOS", "DIR_DADOS_TRATADOS", "DIR_EVIDENCIAS", "DIR_RELATORIOS", "DIR_LOGS"):
            mp.setattr(config, nome, pasta / nome.lower())

        from sompo import pipeline
        resumo = pipeline.executar(n_equipamentos=40, dias=120, gerar_relatorio=True)
        yield {"pasta": pasta, "resumo": resumo}


@pytest.fixture(scope="session")
def resumo_pipeline(ambiente):
    return ambiente["resumo"]


@pytest.fixture(scope="session")
def cliente(ambiente):
    from fastapi.testclient import TestClient
    from sompo.api import app
    with TestClient(app) as c:
        yield c


def cabecalho(perfil: str) -> dict:
    return {"X-API-Key": CHAVES[perfil]}


def leitura_exemplo(id_eq: str = "EQ-900", data_hora: str = "2026-09-01 10:00:00", **extra) -> dict:
    base = {
        "id_equipamento": id_eq, "data_hora": data_hora, "tipo_equipamento": "Trator", "regiao": "PR",
        "tipo_operacao": "Plantio", "idade_anos": 5, "horas_uso_continuo": 8.0, "rpm_medio": 1900,
        "temperatura_motor_c": 88.0, "pressao_oleo_psi": 45.0, "vibracao_mm_s": 4.2, "carga_motor_pct": 60.0,
        "temperatura_ambiente_c": 25.0, "umidade_relativa_pct": 55.0, "declividade_terreno_pct": 8.0,
    }
    base.update(extra)
    return base


def leitura_critica(id_eq: str = "EQ-950", data_hora: str = "2026-09-01 11:00:00") -> dict:
    return leitura_exemplo(id_eq, data_hora, tipo_equipamento="Colheitadeira", tipo_operacao="Colheita",
                           idade_anos=17, horas_uso_continuo=14, temperatura_motor_c=104, pressao_oleo_psi=19,
                           vibracao_mm_s=10.5, carga_motor_pct=95, rpm_medio=2450)
