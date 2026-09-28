"""Simulador de telemetria da frota (dados operacionais + ambientais).

Evolução em relação à Sprint 2: lá o rótulo "houve_quebra" era uma regra fixa
(idade > 8 e temperatura > 95), então o modelo só "decorava" a regra. Aqui a
falha é um evento PROBABILÍSTICO que depende do desgaste acumulado de cada
máquina ao longo do tempo, das condições de operação e do ambiente. O alvo
passa a ser "falha nos próximos 7 dias", que é o que permite agir antes.

Também existe `injetar_inconsistencias`, que suja os dados de propósito
(ausentes, duplicados, erros de sensor, textos fora do padrão) para provar
que o pipeline de tratamento funciona com dados do mundo real.
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from sompo import config

CLIMA_REGIAO = {"MT": 31, "GO": 29, "MS": 28, "BA": 31, "MG": 26, "SP": 26, "PR": 23, "RS": 21}
RELEVO_REGIAO = {"MT": 4, "GO": 5, "MS": 4, "BA": 5, "MG": 14, "SP": 8, "PR": 11, "RS": 12}
CARGA_OPERACAO = {"Preparo de solo": 80, "Colheita": 75, "Plantio": 65, "Pulverização": 55, "Transporte": 50}


def _sigmoide(z: float) -> float:
    return 1.0 / (1.0 + np.exp(-z))


def simular_frota(n_equipamentos: int = 80, dias: int = 150, data_inicio: str = "2026-04-01",
                  semente: int = config.SEMENTE) -> pd.DataFrame:
    """Gera o histórico diário de telemetria da frota, já rotulado com `falha_7d`."""
    rng = np.random.default_rng(semente)
    inicio = datetime.fromisoformat(data_inicio)
    linhas, falhas = [], []

    for i in range(1, n_equipamentos + 1):
        id_eq = f"EQ-{i:03d}"
        tipo = rng.choice(config.TIPOS_EQUIPAMENTO, p=[0.55, 0.25, 0.20])
        regiao = rng.choice(config.REGIOES)
        idade = int(rng.integers(1, 21))
        qualidade_manutencao = rng.uniform(0.6, 1.5)  # >1 = manutenção descuidada, desgasta mais rápido
        desgaste = rng.uniform(0.0, 0.35)

        for d in range(dias):
            if rng.random() > 0.85:  # máquina parada nesse dia
                continue
            operacao = rng.choice(config.OPERACOES_POR_EQUIPAMENTO[tipo])
            horas = float(np.clip(rng.normal(9, 3), 1, 20))
            carga = float(np.clip(rng.normal(CARGA_OPERACAO[operacao], 12), 20, 100))
            rpm = float(np.clip(1300 + carga * 10 + rng.normal(0, 150), 800, 3000))
            temp_amb = float(CLIMA_REGIAO[regiao] + 3 * np.sin(2 * np.pi * d / 120) + rng.normal(0, 2.5))
            umidade = float(np.clip(rng.normal(60 - (temp_amb - 25) * 1.5, 12), 15, 100))
            declividade = float(np.clip(abs(rng.normal(RELEVO_REGIAO[regiao], 4)), 0, 40))
            vibracao = float(np.clip(2.0 + 0.12 * idade + 2 * desgaste + 5 * desgaste ** 2 + 0.08 * declividade + rng.normal(0, 0.4), 0.5, 25))
            pressao = float(np.clip(55 - 8 * desgaste - 18 * desgaste ** 2 - 0.6 * idade + rng.normal(0, 2), 5, 90))
            temp_motor = float(70 + 0.18 * carga + 0.4 * (temp_amb - 25) + 0.5 * horas + 4 * desgaste + 10 * desgaste ** 2 + rng.normal(0, 2.5))

            # Probabilidade de falha no dia (o "mundo real" que o modelo precisa descobrir)
            z = (-9.5 + 8.5 * desgaste
                 + 0.15 * max(temp_motor - 92, 0)
                 + 0.70 * max(vibracao - 5.5, 0)
                 + 0.12 * max(32 - pressao, 0)
                 + 0.04 * idade
                 + 0.03 * max(carga - 85, 0)
                 + 0.05 * max(horas - 12, 0)
                 + (0.04 * declividade if tipo == "Colheitadeira" else 0.0))
            falhou = rng.random() < _sigmoide(z)

            data_hora = inicio + timedelta(days=d, hours=int(rng.integers(6, 18)), minutes=int(rng.integers(0, 60)))
            linhas.append({
                "id_equipamento": id_eq, "data_hora": data_hora.strftime("%Y-%m-%d %H:%M:%S"), "dia": d,
                "tipo_equipamento": tipo, "regiao": regiao, "tipo_operacao": operacao,
                "idade_anos": idade, "horas_uso_continuo": round(horas, 1), "rpm_medio": round(rpm, 0),
                "temperatura_motor_c": round(temp_motor, 1), "pressao_oleo_psi": round(pressao, 1),
                "vibracao_mm_s": round(vibracao, 2), "carga_motor_pct": round(carga, 1),
                "temperatura_ambiente_c": round(temp_amb, 1), "umidade_relativa_pct": round(umidade, 1),
                "declividade_terreno_pct": round(declividade, 1),
            })

            # Desgaste acumula com o esforço e ACELERA perto da falha (curva P-F);
            # manutenção preventiva/corretiva reduz
            desgaste += 0.0075 * (horas / 9) * (carga / 70) * qualidade_manutencao * (1 + 4 * desgaste ** 2)
            if falhou:
                falhas.append((id_eq, d))
                desgaste = 0.05  # manutenção corretiva após a quebra
            elif rng.random() < 0.012:
                desgaste *= 0.3  # manutenção preventiva ocasional

    df = pd.DataFrame(linhas)
    df[config.ALVO] = _rotular_janela(df, falhas, dias)
    return df.drop(columns=["dia"])


def _rotular_janela(df: pd.DataFrame, falhas: list, dias: int) -> pd.Series:
    """falha_7d = 1 se houve falha do equipamento em [dia, dia + 7).

    Leituras dos últimos 7 dias ficam sem rótulo (NaN): ainda não sabemos o
    futuro delas (janela censurada), então não podem ser usadas no treino.
    """
    dias_falha: dict[str, list[int]] = {}
    for eq, d in falhas:
        dias_falha.setdefault(eq, []).append(d)

    rotulos = []
    for eq, d in zip(df["id_equipamento"], df["dia"]):
        if any(d <= f < d + config.HORIZONTE_DIAS for f in dias_falha.get(eq, [])):
            rotulos.append(1.0)
        elif d >= dias - config.HORIZONTE_DIAS:
            rotulos.append(np.nan)
        else:
            rotulos.append(0.0)
    return pd.Series(rotulos, index=df.index)


def gerar_leitura_ao_vivo(rng: np.random.Generator, id_equipamento: str, tipo: str, regiao: str,
                          idade: int, estressada: bool = False) -> dict:
    """Gera uma leitura avulsa (sem rótulo), como a que chega de um sensor em campo."""
    operacao = str(rng.choice(config.OPERACOES_POR_EQUIPAMENTO[tipo]))
    desgaste = rng.uniform(0.6, 1.0) if estressada else rng.uniform(0.0, 0.3)
    horas = float(np.clip(rng.normal(13 if estressada else 8, 2), 1, 20))
    carga = float(np.clip(rng.normal(92 if estressada else CARGA_OPERACAO[operacao], 6), 20, 100))
    temp_amb = float(CLIMA_REGIAO[regiao] + rng.normal(0, 2))
    return {
        "id_equipamento": id_equipamento, "tipo_equipamento": tipo, "regiao": regiao,
        "tipo_operacao": operacao, "idade_anos": idade,
        "horas_uso_continuo": round(horas, 1),
        "rpm_medio": round(float(np.clip(1300 + carga * 10 + rng.normal(0, 120), 800, 3000)), 0),
        "temperatura_motor_c": round(70 + 0.18 * carga + 0.4 * (temp_amb - 25) + 0.5 * horas + 15 * desgaste + rng.normal(0, 2), 1),
        "pressao_oleo_psi": round(float(np.clip(55 - 25 * desgaste - 0.6 * idade + rng.normal(0, 2), 5, 90)), 1),
        "vibracao_mm_s": round(float(np.clip(2 + 0.12 * idade + 6 * desgaste + rng.normal(0, 0.5), 0.5, 25)), 2),
        "carga_motor_pct": round(carga, 1),
        "temperatura_ambiente_c": round(temp_amb, 1),
        "umidade_relativa_pct": round(float(np.clip(rng.normal(55, 10), 15, 100)), 1),
        "declividade_terreno_pct": round(float(np.clip(abs(rng.normal(RELEVO_REGIAO[regiao], 4)), 0, 40)), 1),
    }


def injetar_inconsistencias(df: pd.DataFrame, semente: int = config.SEMENTE) -> pd.DataFrame:
    """Suja o dataset de propósito para simular problemas reais de coleta."""
    rng = np.random.default_rng(semente + 1)
    sujo = df.copy()
    sujo = sujo.astype({c: "object" for c in ["regiao", "id_equipamento", "tipo_operacao", "data_hora"]})
    n = len(sujo)

    def amostra(frac):
        return rng.choice(sujo.index, size=max(1, int(n * frac)), replace=False)

    # 1) Valores ausentes (sensor sem sinal)
    for col in ["temperatura_motor_c", "pressao_oleo_psi", "vibracao_mm_s", "umidade_relativa_pct", "carga_motor_pct"]:
        sujo.loc[amostra(0.02), col] = np.nan
    # 2) Leituras com vários sensores fora do ar ao mesmo tempo (devem ser rejeitadas)
    idx = amostra(0.003)
    sujo.loc[idx, ["temperatura_motor_c", "pressao_oleo_psi", "vibracao_mm_s", "rpm_medio"]] = np.nan
    # 3) Erros de sensor (valores fisicamente impossíveis)
    sujo.loc[amostra(0.004), "temperatura_motor_c"] = -999.0
    sujo.loc[amostra(0.003), "rpm_medio"] = -1.0
    sujo.loc[amostra(0.002), "umidade_relativa_pct"] = 150.0
    # 4) Textos fora do padrão
    idx = amostra(0.03)
    sujo.loc[idx, "regiao"] = [rng.choice([f" {r.lower()} ", r.lower(), {"MT": "Mato Grosso", "PR": "Paraná", "RS": "Rio Grande do Sul"}.get(r, r)]) for r in sujo.loc[idx, "regiao"]]
    idx = amostra(0.02)
    sujo.loc[idx, "id_equipamento"] = [f" {v.lower()}" for v in sujo.loc[idx, "id_equipamento"]]
    idx = amostra(0.02)
    sujo.loc[idx, "tipo_operacao"] = [v.lower().replace("ç", "c").replace("ã", "a") for v in sujo.loc[idx, "tipo_operacao"]]
    # 5) Datas corrompidas
    sujo.loc[amostra(0.002), "data_hora"] = "data_invalida"
    # 6) Registros duplicados (reenvio do mesmo pacote pelo sensor)
    duplicados = sujo.loc[amostra(0.015)]
    sujo = pd.concat([sujo, duplicados], ignore_index=True)
    return sujo.sample(frac=1, random_state=semente).reset_index(drop=True)
