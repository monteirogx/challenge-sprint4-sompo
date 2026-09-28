"""Transforma a probabilidade do modelo em decisão: nível, alerta e recomendações.

Os critérios são explícitos (config.NIVEIS_RISCO, LIMITES_OPERACIONAIS e
REGRAS_SEGURANCA) para que qualquer usuário entenda POR QUE um alerta foi gerado.
"""
import operator

from sompo import config

_OPERADORES = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}

NOMES_AMIGAVEIS = {
    "idade_anos": "Idade do equipamento",
    "horas_uso_continuo": "Horas de uso contínuo",
    "rpm_medio": "RPM médio",
    "temperatura_motor_c": "Temperatura do motor",
    "pressao_oleo_psi": "Pressão do óleo",
    "vibracao_mm_s": "Vibração",
    "carga_motor_pct": "Carga do motor",
    "temperatura_ambiente_c": "Temperatura ambiente",
    "umidade_relativa_pct": "Umidade relativa",
    "declividade_terreno_pct": "Declividade do terreno",
}


def classificar_nivel(score: float) -> str:
    for nivel, minimo in config.NIVEIS_RISCO:
        if score >= minimo:
            return nivel
    return "BAIXO"


def verificar_regras_seguranca(leitura: dict) -> list[str]:
    """Condições extremas que forçam CRÍTICO mesmo que o modelo discorde."""
    return [descricao for campo, (op, limite, descricao) in config.REGRAS_SEGURANCA.items()
            if leitura.get(campo) is not None and _OPERADORES[op](leitura[campo], limite)]


def limites_violados(leitura: dict) -> list[dict]:
    violados = []
    for campo, (op, limite, descricao, componente) in config.LIMITES_OPERACIONAIS.items():
        valor = leitura.get(campo)
        if valor is not None and _OPERADORES[op](valor, limite):
            violados.append({"variavel": campo, "valor": valor, "limite": f"{op} {limite}",
                             "descricao": descricao, "componente": componente})
    return violados


def avaliar(leitura: dict, probabilidade: float, contribuicoes: list[dict]) -> dict:
    """Consolida score, nível, fatores e recomendações de uma leitura."""
    score = round(float(probabilidade) * 100, 1)
    nivel = classificar_nivel(score)
    regras = verificar_regras_seguranca(leitura)
    if regras:
        nivel = "CRÍTICO"
    violados = limites_violados(leitura)
    fatores = {
        "principais_contribuicoes_modelo": [
            {**c, "nome": NOMES_AMIGAVEIS.get(c["variavel"], c["variavel"])} for c in contribuicoes],
        "limites_operacionais_violados": violados,
        "regras_seguranca_acionadas": regras,
    }
    return {
        "score_risco": score,
        "nivel_risco": nivel,
        "alerta": nivel in config.NIVEIS_ALERTA,
        "regra_seguranca": "; ".join(regras) or None,
        "fatores": fatores,
        "recomendacoes": gerar_recomendacoes(nivel, contribuicoes, violados, regras),
    }


def _componentes_suspeitos(contribuicoes: list[dict], violados: list[dict]) -> list[str]:
    componentes = [v["componente"] for v in violados]
    for c in contribuicoes:
        limite = config.LIMITES_OPERACIONAIS.get(c["variavel"])
        if limite and limite[3] not in componentes:
            componentes.append(limite[3])
    return componentes[:3]


def gerar_recomendacoes(nivel: str, contribuicoes: list[dict], violados: list[dict], regras: list[str]) -> dict:
    """Uma recomendação objetiva para cada perfil de usuário das User Stories."""
    componentes = _componentes_suspeitos(contribuicoes, violados)
    alvo = ", ".join(componentes) if componentes else "itens do checklist de revisão periódica"
    causa = "; ".join(regras) if regras else (violados[0]["descricao"] if violados else "combinação de fatores de desgaste")

    if nivel == "CRÍTICO":
        return {
            "operador": f"PARE a máquina assim que for seguro e acione a manutenção. Motivo: {causa}.",
            "tecnico": f"Inspeção imediata (até 24 h), antes de liberar a máquina: {alvo}.",
            "gestor": "Retire o equipamento da escala e aloque uma máquina reserva; risco alto de quebra nos próximos 7 dias.",
            "analista": "Exposição elevada a sinistro: registre o alerta e acompanhe se a intervenção preventiva é feita.",
        }
    if nivel == "ALTO":
        return {
            "operador": "Reduza carga e rotação, faça pausas de resfriamento e avise o supervisor ao fim do turno.",
            "tecnico": f"Agende inspeção em até 72 h com foco em: {alvo}.",
            "gestor": "Programe a parada preventiva na janela de menor demanda desta semana.",
            "analista": "Risco acima do normal: monitore a tendência do equipamento e a execução da manutenção.",
        }
    if nivel == "MODERADO":
        return {
            "operador": "Operação liberada, com atenção aos indicadores do painel.",
            "tecnico": f"Inclua na próxima revisão programada: {alvo}.",
            "gestor": "Acompanhe a tendência do equipamento nas próximas leituras.",
            "analista": "Sem ação imediata; equipamento em observação.",
        }
    return {
        "operador": "Operação liberada.",
        "tecnico": "Manter o plano de manutenção preventiva.",
        "gestor": "Nenhuma ação necessária.",
        "analista": "Risco dentro do esperado.",
    }
