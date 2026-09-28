"""Configuração central do sistema.

Tudo que é parâmetro de negócio (faixas válidas, limites operacionais,
critérios dos níveis de risco) fica aqui, em um único lugar, para que as
regras sejam explícitas, auditáveis e fáceis de ajustar.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[2]

# O .env nunca é versionado (ver .gitignore). Procura na raiz e em config/.
for _env in (RAIZ / ".env", RAIZ / "config" / ".env"):
    if _env.is_file():
        load_dotenv(_env, override=False)
        break

SEMENTE = 42
VERSAO_SISTEMA = "4.0.0"

# ==========================================
# 1. CAMINHOS
# ==========================================
DIR_DADOS_BRUTOS = RAIZ / "data" / "raw"
DIR_DADOS_TRATADOS = RAIZ / "data" / "processed"
DIR_MODELOS = RAIZ / "models"
DIR_LOGS = RAIZ / "logs"
DIR_RELATORIOS = RAIZ / "reports"
DIR_EVIDENCIAS = RAIZ / "docs" / "evidencias"


def caminho_banco() -> Path:
    """Lido a cada chamada para permitir que os testes usem um banco temporário."""
    return Path(os.getenv("SOMPO_DB_PATH", RAIZ / "data" / "sompo_mvp.db"))


def caminho_modelo() -> Path:
    return Path(os.getenv("SOMPO_MODELO_PATH", DIR_MODELOS / "modelo_risco.joblib"))


def caminho_metricas() -> Path:
    return caminho_modelo().with_name("metricas_modelo.json")


# ==========================================
# 2. DOMÍNIO (valores aceitos)
# ==========================================
TIPOS_EQUIPAMENTO = ["Trator", "Colheitadeira", "Pulverizador"]
REGIOES = ["MT", "GO", "MS", "BA", "MG", "SP", "PR", "RS"]
TIPOS_OPERACAO = ["Preparo de solo", "Plantio", "Pulverização", "Colheita", "Transporte"]

# Quais operações cada tipo de máquina realiza (usado pelo simulador e na validação)
OPERACOES_POR_EQUIPAMENTO = {
    "Trator": ["Preparo de solo", "Plantio", "Transporte"],
    "Colheitadeira": ["Colheita", "Transporte"],
    "Pulverizador": ["Pulverização", "Transporte"],
}

FEATURES_NUMERICAS = [
    "idade_anos",
    "horas_uso_continuo",
    "rpm_medio",
    "temperatura_motor_c",
    "pressao_oleo_psi",
    "vibracao_mm_s",
    "carga_motor_pct",
    "temperatura_ambiente_c",
    "umidade_relativa_pct",
    "declividade_terreno_pct",
]
FEATURES_CATEGORICAS = ["tipo_equipamento", "regiao", "tipo_operacao"]
ALVO = "falha_7d"  # 1 = a máquina teve falha nos 7 dias seguintes à leitura
HORIZONTE_DIAS = 7

# Faixas fisicamente plausíveis. Fora disso = erro de sensor (valor descartado).
FAIXAS_VALIDAS = {
    "idade_anos": (0, 40),
    "horas_uso_continuo": (0, 24),
    "rpm_medio": (0, 3500),
    "temperatura_motor_c": (-10, 150),
    "pressao_oleo_psi": (0, 100),
    "vibracao_mm_s": (0, 50),
    "carga_motor_pct": (0, 100),
    "temperatura_ambiente_c": (-10, 55),
    "umidade_relativa_pct": (0, 100),
    "declividade_terreno_pct": (0, 60),
}

# Leituras com mais campos de sensor ausentes que isso são rejeitadas
MAX_CAMPOS_AUSENTES = 3

# ==========================================
# 3. LIMITES OPERACIONAIS (explicabilidade)
# ==========================================
# (comparador, limite, descrição, componente a inspecionar)
LIMITES_OPERACIONAIS = {
    "temperatura_motor_c": (">", 98, "Temperatura do motor acima de 98 °C", "sistema de arrefecimento (radiador, bomba d'água, líquido)"),
    "vibracao_mm_s": (">", 7.1, "Vibração acima de 7,1 mm/s (zona de alerta ISO 10816)", "rolamentos, eixos e transmissão"),
    "pressao_oleo_psi": ("<", 25, "Pressão do óleo abaixo de 25 psi", "sistema de lubrificação (nível, filtro e bomba de óleo)"),
    "rpm_medio": (">", 2400, "RPM médio acima de 2.400", "regime de operação do motor (sobrerrotação)"),
    "carga_motor_pct": (">", 90, "Carga do motor acima de 90%", "dimensionamento do implemento / sobrecarga"),
    "horas_uso_continuo": (">", 12, "Mais de 12 h de uso contínuo", "jornada da máquina (pausas de resfriamento)"),
    "declividade_terreno_pct": (">", 20, "Terreno com declividade acima de 20%", "estabilidade e esforço de tração"),
    "temperatura_ambiente_c": (">", 38, "Temperatura ambiente acima de 38 °C", "arrefecimento sob calor extremo"),
    "idade_anos": (">=", 15, "Equipamento com 15 anos ou mais", "revisão geral por envelhecimento"),
}

# Regras de segurança: condições que forçam nível CRÍTICO independentemente
# do modelo (proteção contra o modelo "não enxergar" um caso extremo).
REGRAS_SEGURANCA = {
    "temperatura_motor_c": (">=", 110, "Superaquecimento severo (>= 110 °C)"),
    "pressao_oleo_psi": ("<=", 12, "Pressão de óleo crítica (<= 12 psi)"),
    "vibracao_mm_s": (">=", 11.2, "Vibração na zona de dano ISO 10816 (>= 11,2 mm/s)"),
}

# ==========================================
# 4. CRITÉRIOS DOS NÍVEIS DE RISCO
# ==========================================
# Score = probabilidade de falha em 7 dias x 100. Limite inferior de cada nível.
# Cortes definidos a partir da curva precisão x recall no período de teste
# (ver models/metricas_modelo.json -> desempenho_por_nivel):
#   ALTO (>= 25): captura ~3 de cada 4 falhas com ~70% dos alertas corretos;
#   CRÍTICO (>= 60): poucos falsos alarmes (~85% de precisão) -> justifica parar a máquina;
#   MODERADO (>= 10): rede de segurança que captura ~90% das falhas, só para acompanhamento.
NIVEIS_RISCO = [
    ("CRÍTICO", 60),
    ("ALTO", 25),
    ("MODERADO", 10),
    ("BAIXO", 0),
]
NIVEIS_ALERTA = {"ALTO", "CRÍTICO"}  # níveis que geram alerta ativo

# ==========================================
# 5. SEGURANÇA (perfis e permissões)
# ==========================================
PERFIS = ["operador", "tecnico", "gestor", "analista", "admin"]
PERMISSOES = {
    "operador": {"enviar_telemetria", "ver_alertas"},
    "tecnico": {"enviar_telemetria", "ver_alertas", "tratar_alertas", "ver_relatorios"},
    "gestor": {"ver_alertas", "tratar_alertas", "ver_relatorios"},
    "analista": {"ver_alertas", "ver_relatorios", "ver_modelo", "ver_auditoria"},
    "admin": {"enviar_telemetria", "ver_alertas", "tratar_alertas", "ver_relatorios", "ver_modelo", "ver_auditoria"},
}


def chaves_api() -> dict[str, str]:
    """Lê as chaves de cada perfil do ambiente (SOMPO_API_KEY_<PERFIL>)."""
    chaves = {}
    for perfil in PERFIS:
        valor = (os.getenv(f"SOMPO_API_KEY_{perfil.upper()}") or "").strip()
        if valor:
            chaves[perfil] = valor
    return chaves


def segredo_pseudonimizacao() -> str:
    return (os.getenv("SOMPO_SEGREDO_PSEUDONIMO") or "segredo-apenas-para-desenvolvimento").strip()
