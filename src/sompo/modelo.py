"""Treino, avaliação, persistência e explicação do modelo de risco.

Decisões técnicas (justificadas no README):
- Divisão TEMPORAL treino/teste: o modelo é avaliado em semanas que ele nunca
  viu, como acontece em produção. Divisão aleatória vazaria informação, pois
  leituras do mesmo equipamento em dias vizinhos são quase idênticas.
- Métrica principal: PR-AUC (average precision). Com ~10% de falhas, a acurácia
  engana (um modelo que diz sempre "não quebra" teria ~90%). Também reportamos
  ROC-AUC, recall/precisão no limiar de alerta e Brier (qualidade da probabilidade,
  importante porque o score é exibido como % de risco).
- Três algoritmos candidatos + o baseline da Sprint 2 (4 variáveis), escolhidos
  pela PR-AUC em uma janela de validação separada do teste.
- Integridade: o SHA-256 do arquivo do modelo é gravado nas métricas e conferido
  a cada carga (modelo adulterado não é usado).
"""
import json
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, brier_score_loss, confusion_matrix,
                             f1_score, precision_score, recall_score, roc_auc_score)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from sompo import config
from sompo.logger import obter_logger
from sompo.seguranca import hash_arquivo

log = obter_logger("modelo")

FEATURES_SPRINT2 = ["idade_anos", "horas_uso_continuo", "rpm_medio", "temperatura_motor_c"]


class ModeloIndisponivel(Exception):
    """Modelo não treinado, corrompido ou com hash divergente."""


# ==========================================
# 1. CONSTRUÇÃO DOS CANDIDATOS
# ==========================================
def _preprocessador(numericas: list[str], categoricas: list[str], escalar: bool) -> ColumnTransformer:
    passos_num = [("imputar", SimpleImputer(strategy="median"))]
    if escalar:
        passos_num.append(("escalar", StandardScaler()))
    transformadores = [("num", Pipeline(passos_num), numericas)]
    if categoricas:
        transformadores.append(("cat", OneHotEncoder(handle_unknown="ignore"), categoricas))
    return ColumnTransformer(transformadores)


def construir_candidatos() -> dict[str, tuple[Pipeline, list[str], list[str]]]:
    num, cat = config.FEATURES_NUMERICAS, config.FEATURES_CATEGORICAS
    return {
        "baseline_sprint2_random_forest": (
            Pipeline([("prep", _preprocessador(FEATURES_SPRINT2, [], False)),
                      ("clf", RandomForestClassifier(n_estimators=200, random_state=config.SEMENTE, n_jobs=-1))]),
            FEATURES_SPRINT2, []),
        "regressao_logistica": (
            Pipeline([("prep", _preprocessador(num, cat, True)),
                      ("clf", LogisticRegression(max_iter=2000, C=0.5))]),
            num, cat),
        "random_forest": (
            Pipeline([("prep", _preprocessador(num, cat, False)),
                      ("clf", RandomForestClassifier(n_estimators=300, min_samples_leaf=10, max_features="sqrt",
                                                     random_state=config.SEMENTE, n_jobs=-1))]),
            num, cat),
        "gradient_boosting": (
            Pipeline([("prep", _preprocessador(num, cat, False)),
                      ("clf", HistGradientBoostingClassifier(learning_rate=0.05, max_iter=300, max_leaf_nodes=15,
                                                             min_samples_leaf=40, l2_regularization=1.0,
                                                             early_stopping=True, validation_fraction=0.15,
                                                             random_state=config.SEMENTE))]),
            num, cat),
    }


# ==========================================
# 2. DIVISÃO TEMPORAL E MÉTRICAS
# ==========================================
def dividir_temporal(df: pd.DataFrame, frac_final: float = 0.2) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    datas = pd.to_datetime(df["data_hora"])
    corte = datas.quantile(1 - frac_final).normalize()
    return df[datas < corte].copy(), df[datas >= corte].copy(), corte.strftime("%Y-%m-%d")


def calcular_metricas(y_real, prob, limiar_alerta: float) -> dict:
    y_real = np.asarray(y_real).astype(int)
    pred = (prob >= limiar_alerta).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_real, pred, labels=[0, 1]).ravel()
    return {
        "pr_auc": round(float(average_precision_score(y_real, prob)), 4),
        "roc_auc": round(float(roc_auc_score(y_real, prob)), 4),
        "brier": round(float(brier_score_loss(y_real, prob)), 4),
        "limiar_alerta": limiar_alerta,
        "recall_alerta": round(float(recall_score(y_real, pred, zero_division=0)), 4),
        "precisao_alerta": round(float(precision_score(y_real, pred, zero_division=0)), 4),
        "f1_alerta": round(float(f1_score(y_real, pred, zero_division=0)), 4),
        "matriz_confusao": {"verdadeiro_negativo": int(tn), "falso_positivo": int(fp),
                            "falso_negativo": int(fn), "verdadeiro_positivo": int(tp)},
        "acuracia": round(float((tp + tn) / len(y_real)), 4),
        "taxa_base_falha": round(float(y_real.mean()), 4),
    }


def _limiar_nivel(nome: str) -> float:
    return dict(config.NIVEIS_RISCO)[nome] / 100


# ==========================================
# 3. TREINO
# ==========================================
def treinar(df: pd.DataFrame) -> tuple[dict, dict]:
    """Treina os candidatos, escolhe o melhor e devolve (pacote_modelo, metricas)."""
    df = df.dropna(subset=[config.ALVO]).copy()
    df[config.ALVO] = df[config.ALVO].astype(int)
    treino, teste, corte_teste = dividir_temporal(df, 0.2)
    sub_treino, validacao, corte_val = dividir_temporal(treino, 0.2)
    limiar = _limiar_nivel("ALTO")
    log.info("Treino: %d | Validação: %d | Teste: %d (a partir de %s)", len(sub_treino), len(validacao), len(teste), corte_teste)

    comparacao = {}
    for nome, (pipe, num, cat) in construir_candidatos().items():
        cols = num + cat
        pipe.fit(sub_treino[cols], sub_treino[config.ALVO])
        m_val = calcular_metricas(validacao[config.ALVO], pipe.predict_proba(validacao[cols])[:, 1], limiar)
        pipe.fit(treino[cols], treino[config.ALVO])  # re-treina com toda a janela de treino
        m_teste = calcular_metricas(teste[config.ALVO], pipe.predict_proba(teste[cols])[:, 1], limiar)
        comparacao[nome] = {"validacao": m_val, "teste": m_teste}
        log.info("%-32s PR-AUC val=%.3f teste=%.3f", nome, m_val["pr_auc"], m_teste["pr_auc"])

    elegiveis = {k: v for k, v in comparacao.items() if not k.startswith("baseline")}
    escolhido = max(elegiveis, key=lambda k: elegiveis[k]["validacao"]["pr_auc"])
    pipe, num, cat = construir_candidatos()[escolhido]
    cols = num + cat
    pipe.fit(treino[cols], treino[config.ALVO])
    prob_teste = pipe.predict_proba(teste[cols])[:, 1]

    importancia = permutation_importance(pipe, teste[cols], teste[config.ALVO], scoring="average_precision",
                                         n_repeats=5, random_state=config.SEMENTE, n_jobs=1)
    ranking = sorted(zip(cols, importancia.importances_mean), key=lambda x: -x[1])

    desempenho_por_nivel = {}
    for nivel, minimo in config.NIVEIS_RISCO:
        if minimo > 0:
            desempenho_por_nivel[f"score>={minimo} ({nivel})"] = calcular_metricas(teste[config.ALVO], prob_teste, minimo / 100)

    # Mediana de máquinas saudáveis: referência para explicar cada score
    saudaveis = treino[treino[config.ALVO] == 0]
    referencia = {c: float(saudaveis[c].median()) for c in config.FEATURES_NUMERICAS}

    versao = f"4.0-{escolhido}-{datetime.now():%Y%m%d%H%M}"
    pacote = {"pipeline": pipe, "versao": versao, "algoritmo": escolhido,
              "features_numericas": num, "features_categoricas": cat, "referencia": referencia}
    metricas = {
        "versao_modelo": versao, "algoritmo_escolhido": escolhido,
        "criterio_escolha": "maior PR-AUC na janela de validação (temporal)",
        "treinado_em": datetime.now().isoformat(timespec="seconds"),
        "linhas": {"treino": len(treino), "teste": len(teste)},
        "periodo_teste_a_partir_de": corte_teste, "periodo_validacao_a_partir_de": corte_val,
        "teste": calcular_metricas(teste[config.ALVO], prob_teste, limiar),
        "desempenho_por_nivel": desempenho_por_nivel,
        "comparacao_modelos": comparacao,
        "importancia_variaveis": [{"variavel": v, "queda_pr_auc": round(float(i), 4)} for v, i in ranking],
        "referencia_maquina_saudavel": referencia,
    }
    return pacote, metricas


def salvar(pacote: dict, metricas: dict) -> None:
    caminho = config.caminho_modelo()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pacote, caminho)
    metricas["sha256_modelo"] = hash_arquivo(caminho)
    config.caminho_metricas().write_text(json.dumps(metricas, indent=2, ensure_ascii=False), encoding="utf-8")
    _cache.clear()
    log.info("Modelo %s salvo em %s", pacote["versao"], caminho)


# ==========================================
# 4. CARGA (com verificação de integridade) E PREDIÇÃO
# ==========================================
_cache: dict = {}


def carregar() -> dict:
    caminho = config.caminho_modelo()
    if not caminho.exists() or not config.caminho_metricas().exists():
        raise ModeloIndisponivel("Modelo ainda não treinado. Execute: python main.py pipeline")
    chave = (str(caminho), caminho.stat().st_mtime)
    if chave not in _cache:
        metricas = json.loads(config.caminho_metricas().read_text(encoding="utf-8"))
        if hash_arquivo(caminho) != metricas.get("sha256_modelo"):
            raise ModeloIndisponivel("Hash do modelo não confere com o registrado: arquivo possivelmente adulterado.")
        _cache.clear()
        _cache[chave] = joblib.load(caminho)
        log.info("Modelo %s carregado (hash verificado)", _cache[chave]["versao"])
    return _cache[chave]


def carregar_metricas() -> dict:
    return json.loads(config.caminho_metricas().read_text(encoding="utf-8"))


def prever(pacote: dict, df: pd.DataFrame) -> np.ndarray:
    cols = pacote["features_numericas"] + pacote["features_categoricas"]
    return pacote["pipeline"].predict_proba(df[cols])[:, 1]


def explicar(pacote: dict, df: pd.DataFrame, prob: np.ndarray, top: int = 3) -> list[list[dict]]:
    """Explicação local por substituição: quanto o risco cairia se a variável
    estivesse no valor típico de uma máquina saudável? As que mais derrubam o
    risco são os fatores que mais pesaram no score daquela leitura."""
    cols = pacote["features_numericas"] + pacote["features_categoricas"]
    variaveis = [c for c in pacote["features_numericas"] if c in pacote["referencia"]]
    base = df[cols].reset_index(drop=True)
    copias = []
    for var in variaveis:
        alterado = base.copy()
        alterado[var] = pacote["referencia"][var]
        copias.append(alterado)
    prob_alterada = pacote["pipeline"].predict_proba(pd.concat(copias, ignore_index=True))[:, 1]
    prob_alterada = prob_alterada.reshape(len(variaveis), len(base))

    explicacoes = []
    for i in range(len(base)):
        contrib = [(var, float(prob[i] - prob_alterada[j, i])) for j, var in enumerate(variaveis)]
        contrib = sorted([c for c in contrib if c[1] > 0.01], key=lambda x: -x[1])[:top]
        explicacoes.append([{"variavel": v, "valor": float(base.loc[i, v]),
                             "referencia_saudavel": round(pacote["referencia"][v], 2),
                             "contribuicao_pontos": round(c * 100, 1)} for v, c in contrib])
    return explicacoes
