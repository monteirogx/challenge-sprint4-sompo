"""Orquestração do fluxo de ponta a ponta de uma leitura:

    validar -> completar ausentes -> gravar leitura (com hash) -> modelo -> risco
            -> gravar predição -> registrar auditoria -> responder

A API e o dashboard chamam as MESMAS funções daqui, então a regra de negócio
existe em um único lugar. A leitura é gravada ANTES da predição: se o modelo
falhar, o dado não se perde e pode ser pontuado depois (`pontuar_pendentes`).
"""
import json
from datetime import datetime

import pandas as pd
from pydantic import ValidationError

from sompo import banco, config, modelo, risco
from sompo.logger import obter_logger
from sompo.seguranca import pseudonimizar
from sompo.validacao import LeituraTelemetria

log = obter_logger("servico")


def _completar_ausentes(dados: dict, ausentes: list[str], pacote: dict) -> None:
    """Preenche sensores ausentes com a mediana das últimas leituras do próprio
    equipamento; sem histórico, usa a referência do modelo."""
    if not ausentes:
        return
    historico = banco.consultar(
        f"SELECT {', '.join(ausentes)} FROM leituras WHERE id_equipamento = ? ORDER BY data_hora DESC LIMIT 10",
        (dados["id_equipamento"],))
    for campo in ausentes:
        if not historico.empty and historico[campo].notna().any():
            dados[campo] = float(historico[campo].median())
        else:
            dados[campo] = pacote["referencia"][campo]


def _pontuar(pacote: dict, df: pd.DataFrame) -> list[dict]:
    prob = modelo.prever(pacote, df)
    explicacoes = modelo.explicar(pacote, df, prob)
    return [risco.avaliar(linha, p, exp) for linha, p, exp in zip(df.to_dict("records"), prob, explicacoes)]


def _registro_predicao(id_leitura: int, versao: str, avaliacao: dict) -> dict:
    return {
        "id_leitura": id_leitura, "versao_modelo": versao,
        "score_risco": avaliacao["score_risco"], "nivel_risco": avaliacao["nivel_risco"],
        "regra_seguranca": avaliacao["regra_seguranca"],
        "fatores_json": json.dumps(avaliacao["fatores"], ensure_ascii=False),
        "recomendacao": json.dumps(avaliacao["recomendacoes"], ensure_ascii=False),
        "criado_em": banco.agora(),
    }


def processar_leitura(entrada: dict, perfil: str, origem: str = "api") -> dict:
    """Processa uma leitura. Lança ValidationError / LeituraDuplicada / ModeloIndisponivel."""
    try:
        leitura = LeituraTelemetria.model_validate(entrada)
    except ValidationError as erro:
        banco.registrar_auditoria(perfil, "enviar_telemetria", str(entrada.get("id_equipamento", "?")),
                                  "rejeitada", {"motivo": "validacao", "erros": erro.errors(include_url=False, include_input=False)})
        raise

    ausentes = leitura.campos_ausentes()
    dados = leitura.model_dump(exclude={"operador_id"})
    dados["data_hora"] = (leitura.data_hora or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    dados["operador_pseudonimo"] = pseudonimizar(leitura.operador_id)
    dados["qtd_campos_imputados"] = len(ausentes)

    pacote = modelo.carregar()  # falha cedo se o modelo estiver indisponível/adulterado
    _completar_ausentes(dados, ausentes, pacote)

    try:
        id_leitura = banco.inserir_leitura(dados, origem)
    except banco.LeituraDuplicada:
        banco.registrar_auditoria(perfil, "enviar_telemetria", dados["id_equipamento"], "rejeitada",
                                  {"motivo": "duplicada", "data_hora": dados["data_hora"]})
        raise

    avaliacao = _pontuar(pacote, pd.DataFrame([dados]))[0]
    banco.salvar_predicoes([_registro_predicao(id_leitura, pacote["versao"], avaliacao)])
    id_predicao = int(banco.consultar("SELECT id_predicao FROM predicoes WHERE id_leitura = ? AND versao_modelo = ?",
                                      (id_leitura, pacote["versao"]))["id_predicao"][0])
    id_alerta, acao_alerta = None, None
    if avaliacao["alerta"]:
        id_alerta, acao_alerta = banco.registrar_alerta(dados["id_equipamento"], id_predicao,
                                                        avaliacao["nivel_risco"], avaliacao["score_risco"])
    banco.registrar_auditoria(perfil, "enviar_telemetria", dados["id_equipamento"], "sucesso", {
        "id_leitura": id_leitura, "id_predicao": id_predicao, "origem": origem, "versao_modelo": pacote["versao"],
        "score_risco": avaliacao["score_risco"], "nivel_risco": avaliacao["nivel_risco"],
        "campos_imputados": ausentes, "regra_seguranca": avaliacao["regra_seguranca"],
        "id_alerta": id_alerta, "acao_alerta": acao_alerta,
    })
    if avaliacao["alerta"]:
        log.warning("ALERTA %s: %s score=%.1f", avaliacao["nivel_risco"], dados["id_equipamento"], avaliacao["score_risco"])

    return {"id_leitura": id_leitura, "id_predicao": id_predicao, "id_equipamento": dados["id_equipamento"],
            "data_hora": dados["data_hora"], "versao_modelo": pacote["versao"], "campos_imputados": ausentes,
            "id_alerta": id_alerta, "acao_alerta": acao_alerta, **avaliacao}


def processar_lote(entradas: list[dict], perfil: str, origem: str = "api") -> dict:
    """Processa várias leituras; o erro de uma não interrompe as demais."""
    resultados, aceitas = [], 0
    for posicao, entrada in enumerate(entradas):
        try:
            r = processar_leitura(entrada, perfil, origem)
            resultados.append({"posicao": posicao, "status": "aceita", "id_leitura": r["id_leitura"],
                               "score_risco": r["score_risco"], "nivel_risco": r["nivel_risco"]})
            aceitas += 1
        except ValidationError as erro:
            resultados.append({"posicao": posicao, "status": "rejeitada",
                               "erros": [e["msg"] for e in erro.errors()]})
        except banco.LeituraDuplicada as erro:
            resultados.append({"posicao": posicao, "status": "duplicada", "erros": [str(erro)]})
    return {"recebidas": len(entradas), "aceitas": aceitas, "rejeitadas": len(entradas) - aceitas,
            "resultados": resultados}


def pontuar_pendentes(tamanho_bloco: int = 2000) -> int:
    """Gera predições para todas as leituras que ainda não têm score da versão atual."""
    pacote = modelo.carregar()
    pendentes = banco.carregar_leituras_sem_predicao(pacote["versao"])
    for inicio in range(0, len(pendentes), tamanho_bloco):
        bloco = pendentes.iloc[inicio:inicio + tamanho_bloco].reset_index(drop=True)
        avaliacoes = _pontuar(pacote, bloco)
        banco.salvar_predicoes([_registro_predicao(int(i), pacote["versao"], a)
                                for i, a in zip(bloco["id_leitura"], avaliacoes)])
    log.info("%d leituras pontuadas com o modelo %s", len(pendentes), pacote["versao"])
    return len(pendentes)


def sincronizar_alertas() -> int:
    """Abre alertas para os equipamentos cuja leitura mais recente está em ALTO/CRÍTICO
    (usado após a carga histórica, que não passa pelo fluxo leitura a leitura)."""
    atual = banco.consultar(
        """SELECT v.id_equipamento, v.id_predicao, v.nivel_risco, v.score_risco FROM vw_risco v
           WHERE v.data_hora = (SELECT MAX(data_hora) FROM leituras l WHERE l.id_equipamento = v.id_equipamento)""")
    em_alerta = atual[atual["nivel_risco"].isin(config.NIVEIS_ALERTA)]
    for r in em_alerta.to_dict("records"):
        banco.registrar_alerta(r["id_equipamento"], int(r["id_predicao"]), r["nivel_risco"], float(r["score_risco"]))
    log.info("%d alertas ativos após sincronização", len(em_alerta))
    return len(em_alerta)
