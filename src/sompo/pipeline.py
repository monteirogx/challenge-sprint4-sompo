"""Pipeline batch de ponta a ponta (reproduzível: mesma semente -> mesmo resultado).

    1. coleta (simulada)  -> data/raw/telemetria_bruta.csv  (com inconsistências)
    2. tratamento         -> data/processed/telemetria_tratada.csv + relatório de qualidade
    3. carga no banco     -> SQLite (com hash por leitura) + conferência de contagem
    4. treino/avaliação   -> models/modelo_risco.joblib + metricas_modelo.json
    5. pontuação          -> score, nível, fatores e recomendações de todas as leituras
    6. relatório          -> reports/relatorio_risco.html + gráficos em docs/evidencias
"""
import json
import time

import pandas as pd

from sompo import banco, config, modelo, relatorios, servico, simulador
from sompo.logger import obter_logger
from sompo.validacao import limpar_lote

log = obter_logger("pipeline")


def executar(n_equipamentos: int = 80, dias: int = 150, gerar_relatorio: bool = True) -> dict:
    inicio = time.perf_counter()
    resumo = {"parametros": {"n_equipamentos": n_equipamentos, "dias": dias, "semente": config.SEMENTE}}

    for pasta in (config.DIR_DADOS_BRUTOS, config.DIR_DADOS_TRATADOS, config.DIR_EVIDENCIAS):
        pasta.mkdir(parents=True, exist_ok=True)

    log.info("[1/6] Coletando telemetria simulada da frota")
    limpo_referencia = simulador.simular_frota(n_equipamentos, dias)
    bruto = simulador.injetar_inconsistencias(limpo_referencia)
    caminho_bruto = config.DIR_DADOS_BRUTOS / "telemetria_bruta.csv"
    bruto.to_csv(caminho_bruto, index=False)

    log.info("[2/6] Tratando inconsistências, ausentes e duplicidades")
    tratado, qualidade = limpar_lote(pd.read_csv(caminho_bruto))
    tratado.to_csv(config.DIR_DADOS_TRATADOS / "telemetria_tratada.csv", index=False)
    resumo["qualidade_dados"] = qualidade

    log.info("[3/6] Carregando no banco de dados")
    banco.inicializar_banco(recriar=True)
    inseridas = banco.inserir_lote(tratado, origem="historico")
    no_banco = int(banco.consultar("SELECT COUNT(*) AS n FROM leituras")["n"][0])
    resumo["carga"] = {"linhas_tratadas": len(tratado), "inseridas": inseridas, "no_banco": no_banco,
                       "sem_perda": len(tratado) == inseridas == no_banco,
                       "integridade_hash": banco.verificar_integridade_leituras()["integra"]}
    if not resumo["carga"]["sem_perda"]:
        raise RuntimeError(f"Perda de dados na carga: {resumo['carga']}")

    log.info("[4/6] Treinando e avaliando o modelo")
    pacote, metricas = modelo.treinar(banco.carregar_dataset_rotulado())
    modelo.salvar(pacote, metricas)
    resumo["modelo"] = {"versao": metricas["versao_modelo"], "algoritmo": metricas["algoritmo_escolhido"],
                        "teste": {k: metricas["teste"][k] for k in ("pr_auc", "roc_auc", "recall_alerta", "precisao_alerta")}}

    log.info("[5/6] Gerando scores de risco")
    resumo["leituras_pontuadas"] = servico.pontuar_pendentes()
    resumo["alertas_abertos"] = servico.sincronizar_alertas()

    banco.registrar_auditoria("sistema", "executar_pipeline", "frota", "sucesso",
                              {"versao_modelo": metricas["versao_modelo"], "leituras": no_banco,
                               "qualidade": qualidade})

    if gerar_relatorio:
        log.info("[6/6] Gerando relatório e gráficos")
        resumo["relatorio"] = relatorios.gerar_relatorio()

    resumo["duracao_segundos"] = round(time.perf_counter() - inicio, 1)
    (config.DIR_EVIDENCIAS / "execucao_pipeline.json").write_text(
        json.dumps(resumo, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("Pipeline concluído em %.1fs", resumo["duracao_segundos"])
    return resumo
