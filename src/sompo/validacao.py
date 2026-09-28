"""Contrato de dados e pipeline de limpeza.

Dois pontos de entrada, com as MESMAS regras de normalização:
- `LeituraTelemetria`: valida uma leitura individual que chega pela API/dashboard.
- `limpar_lote`: trata um lote histórico (CSV) antes de ir para o banco e para o modelo,
  devolvendo também um relatório de qualidade com tudo que foi corrigido ou descartado.
"""
import unicodedata
from datetime import datetime

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sompo import config
from sompo.logger import obter_logger

log = obter_logger("validacao")

# ==========================================
# 1. NORMALIZAÇÃO DE TEXTOS
# ==========================================
_ALIAS_REGIAO = {
    "MATO GROSSO": "MT", "GOIAS": "GO", "MATO GROSSO DO SUL": "MS", "BAHIA": "BA",
    "MINAS GERAIS": "MG", "SAO PAULO": "SP", "PARANA": "PR", "RIO GRANDE DO SUL": "RS",
}


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def normalizar_regiao(valor) -> str | None:
    if valor is None or (isinstance(valor, float) and np.isnan(valor)):
        return None
    chave = _sem_acento(str(valor)).strip().upper()
    chave = _ALIAS_REGIAO.get(chave, chave)
    return chave if chave in config.REGIOES else None


def _normalizar_por_lista(valor, opcoes: list[str]) -> str | None:
    if valor is None or (isinstance(valor, float) and np.isnan(valor)):
        return None
    chave = _sem_acento(str(valor)).strip().lower()
    for opcao in opcoes:
        if _sem_acento(opcao).lower() == chave:
            return opcao
    return None


def normalizar_operacao(valor) -> str | None:
    return _normalizar_por_lista(valor, config.TIPOS_OPERACAO)


def normalizar_tipo_equipamento(valor) -> str | None:
    return _normalizar_por_lista(valor, config.TIPOS_EQUIPAMENTO)


def normalizar_id(valor) -> str | None:
    if valor is None or (isinstance(valor, float) and np.isnan(valor)):
        return None
    texto = str(valor).strip().upper()
    return texto if texto.startswith("EQ-") and texto[3:].isdigit() else None


# ==========================================
# 2. CONTRATO DE UMA LEITURA (API / DASHBOARD)
# ==========================================
def _campo(nome: str, descricao: str):
    minimo, maximo = config.FAIXAS_VALIDAS[nome]
    return Field(default=None, ge=minimo, le=maximo, description=descricao)


class LeituraTelemetria(BaseModel):
    """Leitura de telemetria. Sensores podem vir ausentes (None): o sistema
    completa com a mediana de referência e registra quais campos foram
    imputados, mas rejeita a leitura se faltarem mais que MAX_CAMPOS_AUSENTES."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id_equipamento: str = Field(description="Identificador no formato EQ-000")
    data_hora: datetime | None = Field(default=None, description="Momento da leitura (padrão: agora)")
    tipo_equipamento: str
    regiao: str = Field(description="UF: " + ", ".join(config.REGIOES))
    tipo_operacao: str
    idade_anos: int = Field(ge=0, le=40)
    horas_uso_continuo: float | None = _campo("horas_uso_continuo", "Horas de uso contínuo")
    rpm_medio: float | None = _campo("rpm_medio", "RPM médio do motor")
    temperatura_motor_c: float | None = _campo("temperatura_motor_c", "Temperatura do motor (°C)")
    pressao_oleo_psi: float | None = _campo("pressao_oleo_psi", "Pressão do óleo (psi)")
    vibracao_mm_s: float | None = _campo("vibracao_mm_s", "Vibração RMS (mm/s)")
    carga_motor_pct: float | None = _campo("carga_motor_pct", "Carga do motor (%)")
    temperatura_ambiente_c: float | None = _campo("temperatura_ambiente_c", "Temperatura ambiente (°C)")
    umidade_relativa_pct: float | None = _campo("umidade_relativa_pct", "Umidade relativa (%)")
    declividade_terreno_pct: float | None = _campo("declividade_terreno_pct", "Declividade do terreno (%)")
    operador_id: str | None = Field(default=None, max_length=60,
                                    description="Matrícula do operador (armazenada pseudonimizada)")

    @field_validator("id_equipamento", mode="before")
    @classmethod
    def _validar_id(cls, v):
        normalizado = normalizar_id(v)
        if not normalizado:
            raise ValueError("id_equipamento deve seguir o formato EQ-000")
        return normalizado

    @field_validator("regiao", mode="before")
    @classmethod
    def _validar_regiao(cls, v):
        normalizado = normalizar_regiao(v)
        if not normalizado:
            raise ValueError(f"região inválida; use uma de {config.REGIOES}")
        return normalizado

    @field_validator("tipo_operacao", mode="before")
    @classmethod
    def _validar_operacao(cls, v):
        normalizado = normalizar_operacao(v)
        if not normalizado:
            raise ValueError(f"tipo_operacao inválido; use um de {config.TIPOS_OPERACAO}")
        return normalizado

    @field_validator("tipo_equipamento", mode="before")
    @classmethod
    def _validar_tipo(cls, v):
        normalizado = normalizar_tipo_equipamento(v)
        if not normalizado:
            raise ValueError(f"tipo_equipamento inválido; use um de {config.TIPOS_EQUIPAMENTO}")
        return normalizado

    @model_validator(mode="after")
    def _validar_conjunto(self):
        if self.tipo_operacao not in config.OPERACOES_POR_EQUIPAMENTO[self.tipo_equipamento]:
            raise ValueError(f"{self.tipo_equipamento} não realiza a operação '{self.tipo_operacao}'")
        if len(self.campos_ausentes()) > config.MAX_CAMPOS_AUSENTES:
            raise ValueError(
                f"leitura com {len(self.campos_ausentes())} sensores ausentes "
                f"(máximo {config.MAX_CAMPOS_AUSENTES}): {self.campos_ausentes()}")
        if self.data_hora and self.data_hora.replace(tzinfo=None) > datetime.now().replace(microsecond=0) + pd.Timedelta(minutes=5):
            raise ValueError("data_hora está no futuro")
        return self

    def campos_ausentes(self) -> list[str]:
        return [c for c in config.FEATURES_NUMERICAS if getattr(self, c) is None]


# ==========================================
# 3. LIMPEZA DE LOTE (HISTÓRICO)
# ==========================================
def limpar_lote(df_bruto: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Trata inconsistências, ausentes e duplicidades de um lote de telemetria.

    Retorna (dados_tratados, relatorio_qualidade). Nenhuma etapa interrompe o
    fluxo: linhas problemáticas são corrigidas quando possível ou descartadas
    com o motivo contabilizado no relatório.
    """
    rel = {"linhas_recebidas": int(len(df_bruto))}
    df = df_bruto.copy()

    obrigatorias = ["id_equipamento", "data_hora", "tipo_equipamento", "regiao", "tipo_operacao"] + config.FEATURES_NUMERICAS
    faltando = [c for c in obrigatorias if c not in df.columns]
    if faltando:
        raise ValueError(f"Colunas obrigatórias ausentes no lote: {faltando}")

    # 1) Padroniza textos
    antes = df[["id_equipamento", "regiao", "tipo_operacao", "tipo_equipamento"]].astype(str).copy()
    df["id_equipamento"] = df["id_equipamento"].map(normalizar_id)
    df["regiao"] = df["regiao"].map(normalizar_regiao)
    df["tipo_operacao"] = df["tipo_operacao"].map(normalizar_operacao)
    df["tipo_equipamento"] = df["tipo_equipamento"].map(normalizar_tipo_equipamento)
    depois = df[["id_equipamento", "regiao", "tipo_operacao", "tipo_equipamento"]].astype(str)
    rel["textos_padronizados"] = int(((antes != depois) & (depois != "None")).sum().sum())

    # 2) Converte datas
    df["data_hora"] = pd.to_datetime(df["data_hora"], errors="coerce", format="%Y-%m-%d %H:%M:%S")

    # 3) Descarta linhas sem identificação mínima
    invalida = df[["id_equipamento", "data_hora", "regiao", "tipo_operacao", "tipo_equipamento"]].isna().any(axis=1)
    rel["descartadas_identificacao_invalida"] = int(invalida.sum())
    df = df[~invalida]

    # 4) Remove duplicidades (mesmo equipamento no mesmo instante)
    n = len(df)
    df = df.drop_duplicates(subset=["id_equipamento", "data_hora"], keep="first")
    rel["descartadas_duplicadas"] = int(n - len(df))

    # 5) Valores fora da faixa física = erro de sensor -> vira ausente
    fora = 0
    for col, (minimo, maximo) in config.FAIXAS_VALIDAS.items():
        df[col] = pd.to_numeric(df[col], errors="coerce")
        mascara = (df[col] < minimo) | (df[col] > maximo)
        fora += int(mascara.sum())
        df.loc[mascara, col] = np.nan
    rel["valores_fora_da_faixa_anulados"] = fora

    # 6) Leituras com sensores demais ausentes são rejeitadas
    ausentes = df[config.FEATURES_NUMERICAS].isna().sum(axis=1)
    rejeitar = ausentes > config.MAX_CAMPOS_AUSENTES
    rel["descartadas_excesso_ausentes"] = int(rejeitar.sum())
    df = df[~rejeitar].copy()

    # 7) Imputação: mediana do próprio equipamento, depois mediana geral
    df["qtd_campos_imputados"] = df[config.FEATURES_NUMERICAS].isna().sum(axis=1).astype(int)
    rel["valores_imputados"] = int(df["qtd_campos_imputados"].sum())
    for col in config.FEATURES_NUMERICAS:
        df[col] = df[col].fillna(df.groupby("id_equipamento")[col].transform("median"))
        df[col] = df[col].fillna(df[col].median())

    # 8) Atributos fixos do equipamento devem ser consistentes (usa o valor mais frequente)
    for col in ["tipo_equipamento", "regiao", "idade_anos"]:
        moda = df.groupby("id_equipamento")[col].agg(lambda s: s.mode().iloc[0])
        df[col] = df["id_equipamento"].map(moda)

    df = df.sort_values(["id_equipamento", "data_hora"]).reset_index(drop=True)
    df["data_hora"] = df["data_hora"].dt.strftime("%Y-%m-%d %H:%M:%S")
    rel["linhas_validas"] = int(len(df))
    rel["taxa_aproveitamento_pct"] = round(100 * len(df) / max(rel["linhas_recebidas"], 1), 2)
    log.info("Limpeza concluída: %s", rel)
    return df, rel
