"""Camada de acesso ao banco (SQLite).

Refinamentos em relação à Sprint 3:
- modelo normalizado (equipamentos / leituras / predições / auditoria) com chaves
  estrangeiras e restrições CHECK e UNIQUE (o banco também recusa dado inválido);
- todas as consultas parametrizadas (sem concatenação de SQL -> sem SQL injection);
- conexão via context manager (commit/rollback automático, nunca fica aberta);
- hash SHA-256 em cada leitura e trilha de auditoria encadeada (blockchain-like):
  qualquer alteração manual no banco é detectada por `verificar_integridade_*`.
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from sompo import config
from sompo.logger import obter_logger
from sompo.seguranca import hash_conteudo

log = obter_logger("banco")
_trava_escrita = threading.Lock()
HASH_INICIAL = "0" * 64


class LeituraDuplicada(Exception):
    """A mesma leitura (equipamento + data_hora) já foi recebida antes."""


def _faixa(col: str) -> str:
    minimo, maximo = config.FAIXAS_VALIDAS[col]
    return f"CHECK ({col} BETWEEN {minimo} AND {maximo})"


ESQUEMA = f"""
CREATE TABLE IF NOT EXISTS equipamentos (
    id_equipamento   TEXT PRIMARY KEY CHECK (id_equipamento LIKE 'EQ-%'),
    tipo_equipamento TEXT NOT NULL CHECK (tipo_equipamento IN ({", ".join(f"'{t}'" for t in config.TIPOS_EQUIPAMENTO)})),
    regiao           TEXT NOT NULL CHECK (regiao IN ({", ".join(f"'{r}'" for r in config.REGIOES)})),
    atualizado_em    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leituras (
    id_leitura             INTEGER PRIMARY KEY AUTOINCREMENT,
    id_equipamento         TEXT NOT NULL REFERENCES equipamentos(id_equipamento),
    data_hora              TEXT NOT NULL,
    tipo_operacao          TEXT NOT NULL,
    {", ".join(f"{c} REAL NOT NULL {_faixa(c)}" for c in config.FEATURES_NUMERICAS)},
    qtd_campos_imputados   INTEGER NOT NULL DEFAULT 0,
    falha_7d               INTEGER CHECK (falha_7d IN (0, 1)),
    origem                 TEXT NOT NULL,
    operador_pseudonimo    TEXT,
    hash_leitura           TEXT NOT NULL,
    recebido_em            TEXT NOT NULL,
    UNIQUE (id_equipamento, data_hora)
);

CREATE TABLE IF NOT EXISTS predicoes (
    id_predicao        INTEGER PRIMARY KEY AUTOINCREMENT,
    id_leitura         INTEGER NOT NULL REFERENCES leituras(id_leitura),
    versao_modelo      TEXT NOT NULL,
    score_risco        REAL NOT NULL CHECK (score_risco BETWEEN 0 AND 100),
    nivel_risco        TEXT NOT NULL,
    regra_seguranca    TEXT,
    fatores_json       TEXT NOT NULL,
    recomendacao       TEXT NOT NULL,
    criado_em          TEXT NOT NULL,
    UNIQUE (id_leitura, versao_modelo)
);

CREATE TABLE IF NOT EXISTS auditoria (
    id_evento      INTEGER PRIMARY KEY AUTOINCREMENT,
    data_hora      TEXT NOT NULL,
    perfil         TEXT NOT NULL,
    acao           TEXT NOT NULL,
    recurso        TEXT NOT NULL,
    resultado      TEXT NOT NULL,
    detalhes_json  TEXT NOT NULL,
    hash_anterior  TEXT NOT NULL,
    hash_evento    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alertas (
    id_alerta       INTEGER PRIMARY KEY AUTOINCREMENT,
    id_equipamento  TEXT NOT NULL REFERENCES equipamentos(id_equipamento),
    id_predicao     INTEGER NOT NULL REFERENCES predicoes(id_predicao),
    nivel_risco     TEXT NOT NULL CHECK (nivel_risco IN ('ALTO', 'CRÍTICO')),
    score_risco     REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ABERTO' CHECK (status IN ('ABERTO', 'RECONHECIDO', 'RESOLVIDO')),
    aberto_em       TEXT NOT NULL,
    atualizado_em   TEXT NOT NULL,
    tratado_por     TEXT,
    observacao      TEXT
);

-- No máximo um alerta ativo (não resolvido) por equipamento
CREATE UNIQUE INDEX IF NOT EXISTS ux_alerta_ativo ON alertas (id_equipamento) WHERE status <> 'RESOLVIDO';
CREATE INDEX IF NOT EXISTS idx_leituras_eq_data ON leituras (id_equipamento, data_hora);
CREATE INDEX IF NOT EXISTS idx_predicoes_nivel ON predicoes (nivel_risco);
"""

VIEW_RISCO = """
CREATE VIEW IF NOT EXISTS vw_risco AS
SELECT l.id_leitura, l.id_equipamento, l.data_hora, e.tipo_equipamento, e.regiao, l.tipo_operacao,
       l.idade_anos, l.horas_uso_continuo, l.rpm_medio, l.temperatura_motor_c, l.pressao_oleo_psi,
       l.vibracao_mm_s, l.carga_motor_pct, l.temperatura_ambiente_c, l.umidade_relativa_pct,
       l.declividade_terreno_pct, l.qtd_campos_imputados, l.origem, l.falha_7d,
       p.id_predicao, p.versao_modelo, p.score_risco, p.nivel_risco, p.regra_seguranca, p.fatores_json, p.recomendacao
FROM leituras l
JOIN equipamentos e ON e.id_equipamento = l.id_equipamento
JOIN predicoes p ON p.id_predicao = (SELECT MAX(id_predicao) FROM predicoes WHERE id_leitura = l.id_leitura);
"""


@contextmanager
def conectar():
    caminho = config.caminho_banco()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(caminho, timeout=15)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys = ON")
    try:
        yield conexao
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def inicializar_banco(recriar: bool = False) -> None:
    caminho = config.caminho_banco()
    if recriar and caminho.exists():
        caminho.unlink()
        log.info("Banco anterior removido para recriação: %s", caminho)
    with conectar() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(ESQUEMA + VIEW_RISCO)
    log.info("Banco pronto em %s", caminho)


def agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==========================================
# LEITURAS
# ==========================================
def conteudo_leitura(dados: dict) -> dict:
    """Campos cobertos pelo hash de integridade da leitura (tipos normalizados)."""
    conteudo = {c: float(dados[c]) for c in config.FEATURES_NUMERICAS}
    conteudo.update({k: str(dados[k]) for k in ("id_equipamento", "data_hora", "tipo_operacao")})
    return conteudo


def _salvar_equipamento(con, dados: dict) -> None:
    con.execute(
        """INSERT INTO equipamentos (id_equipamento, tipo_equipamento, regiao, atualizado_em)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(id_equipamento) DO UPDATE SET
               tipo_equipamento = excluded.tipo_equipamento,
               regiao = excluded.regiao, atualizado_em = excluded.atualizado_em""",
        (dados["id_equipamento"], dados["tipo_equipamento"], dados["regiao"], agora()),
    )


_COLUNAS_LEITURA = (["id_equipamento", "data_hora", "tipo_operacao"] + config.FEATURES_NUMERICAS
                    + ["qtd_campos_imputados", "falha_7d", "origem", "operador_pseudonimo", "hash_leitura", "recebido_em"])
_SQL_INSERIR_LEITURA = (f"INSERT INTO leituras ({', '.join(_COLUNAS_LEITURA)}) "
                        f"VALUES ({', '.join('?' * len(_COLUNAS_LEITURA))})")


def _tupla_leitura(dados: dict, origem: str) -> tuple:
    registro = dict(dados)
    registro["hash_leitura"] = hash_conteudo(conteudo_leitura(dados))
    registro["origem"] = origem
    registro["recebido_em"] = agora()
    registro.setdefault("qtd_campos_imputados", 0)
    registro.setdefault("falha_7d", None)
    registro.setdefault("operador_pseudonimo", None)
    if registro["falha_7d"] is not None and pd.isna(registro["falha_7d"]):
        registro["falha_7d"] = None
    valores = []
    for c in _COLUNAS_LEITURA:
        v = registro[c]
        valores.append(v.item() if hasattr(v, "item") else v)  # numpy -> tipo Python
    return tuple(valores)


def inserir_leitura(dados: dict, origem: str) -> int:
    """Grava uma leitura individual (API/dashboard) e devolve o id_leitura."""
    with conectar() as con:
        _salvar_equipamento(con, dados)
        try:
            cursor = con.execute(_SQL_INSERIR_LEITURA, _tupla_leitura(dados, origem))
        except sqlite3.IntegrityError as erro:
            if "UNIQUE" in str(erro):
                raise LeituraDuplicada(
                    f"Leitura de {dados['id_equipamento']} em {dados['data_hora']} já registrada.") from erro
            raise
        return int(cursor.lastrowid)


def inserir_lote(df: pd.DataFrame, origem: str) -> int:
    """Grava um lote tratado. Duplicatas já existentes no banco são ignoradas."""
    registros = df.to_dict("records")
    with conectar() as con:
        for eq in {r["id_equipamento"]: r for r in registros}.values():
            _salvar_equipamento(con, eq)
        antes = con.total_changes
        con.executemany(_SQL_INSERIR_LEITURA.replace("INSERT", "INSERT OR IGNORE", 1),
                        [_tupla_leitura(r, origem) for r in registros])
        inseridas = con.total_changes - antes
    log.info("Lote gravado: %d de %d leituras (origem=%s)", inseridas, len(registros), origem)
    return inseridas


def salvar_predicoes(predicoes: list[dict]) -> None:
    colunas = ["id_leitura", "versao_modelo", "score_risco", "nivel_risco", "regra_seguranca",
               "fatores_json", "recomendacao", "criado_em"]
    with conectar() as con:
        con.executemany(
            f"INSERT OR REPLACE INTO predicoes ({', '.join(colunas)}) VALUES ({', '.join('?' * len(colunas))})",
            [tuple(p.get(c, agora() if c == "criado_em" else None) for c in colunas) for p in predicoes],
        )


def consultar(sql: str, parametros: tuple = ()) -> pd.DataFrame:
    with conectar() as con:
        return pd.read_sql_query(sql, con, params=parametros)


def carregar_dataset_rotulado() -> pd.DataFrame:
    return consultar(
        """SELECT l.*, e.tipo_equipamento, e.regiao
           FROM leituras l JOIN equipamentos e USING (id_equipamento)
           WHERE l.falha_7d IS NOT NULL ORDER BY l.data_hora""")


def carregar_leituras_sem_predicao(versao_modelo: str) -> pd.DataFrame:
    return consultar(
        """SELECT l.*, e.tipo_equipamento, e.regiao
           FROM leituras l JOIN equipamentos e USING (id_equipamento)
           WHERE NOT EXISTS (SELECT 1 FROM predicoes p
                             WHERE p.id_leitura = l.id_leitura AND p.versao_modelo = ?)""",
        (versao_modelo,))


# ==========================================
# ALERTAS (ciclo de vida: ABERTO -> RECONHECIDO -> RESOLVIDO)
# ==========================================
class AlertaNaoEncontrado(Exception):
    """id_alerta inexistente."""


class TransicaoInvalida(Exception):
    """Mudança de status não permitida (ex.: reabrir um alerta resolvido)."""


TRANSICOES_ALERTA = {"ABERTO": {"RECONHECIDO", "RESOLVIDO"}, "RECONHECIDO": {"RESOLVIDO"}, "RESOLVIDO": set()}
_GRAVIDADE = {"ALTO": 1, "CRÍTICO": 2}


def registrar_alerta(id_equipamento: str, id_predicao: int, nivel: str, score: float) -> tuple[int, str]:
    """Abre um alerta ou atualiza o alerta ativo do equipamento (nunca duplica).

    Retorna (id_alerta, acao), com acao em {"criado", "atualizado", "reescalado"}.
    Se o risco subir de ALTO para CRÍTICO num alerta já reconhecido, ele volta a ABERTO.
    """
    with _trava_escrita, conectar() as con:
        ativo = con.execute("SELECT * FROM alertas WHERE id_equipamento = ? AND status <> 'RESOLVIDO'",
                            (id_equipamento,)).fetchone()
        if ativo is None:
            cursor = con.execute(
                """INSERT INTO alertas (id_equipamento, id_predicao, nivel_risco, score_risco, aberto_em, atualizado_em)
                   VALUES (?, ?, ?, ?, ?, ?)""", (id_equipamento, id_predicao, nivel, score, agora(), agora()))
            return int(cursor.lastrowid), "criado"
        reescalar = _GRAVIDADE[nivel] > _GRAVIDADE[ativo["nivel_risco"]] and ativo["status"] == "RECONHECIDO"
        con.execute(
            """UPDATE alertas SET id_predicao = ?, nivel_risco = ?, score_risco = ?, atualizado_em = ?,
                      status = CASE WHEN ? THEN 'ABERTO' ELSE status END
               WHERE id_alerta = ?""",
            (id_predicao, nivel, score, agora(), reescalar, ativo["id_alerta"]))
        return int(ativo["id_alerta"]), "reescalado" if reescalar else "atualizado"


def tratar_alerta(id_alerta: int, novo_status: str, perfil: str, observacao: str | None = None) -> dict:
    """Muda o status de um alerta, respeitando as transições permitidas."""
    with _trava_escrita, conectar() as con:
        alerta = con.execute("SELECT * FROM alertas WHERE id_alerta = ?", (id_alerta,)).fetchone()
        if alerta is None:
            raise AlertaNaoEncontrado(f"Alerta {id_alerta} não existe.")
        if novo_status not in TRANSICOES_ALERTA[alerta["status"]]:
            raise TransicaoInvalida(f"Não é possível passar de {alerta['status']} para {novo_status}.")
        con.execute("UPDATE alertas SET status = ?, tratado_por = ?, observacao = ?, atualizado_em = ? WHERE id_alerta = ?",
                    (novo_status, perfil, observacao, agora(), id_alerta))
        return {"id_alerta": id_alerta, "id_equipamento": alerta["id_equipamento"],
                "status_anterior": alerta["status"], "status": novo_status}


# ==========================================
# AUDITORIA (trilha encadeada por hash)
# ==========================================
def _hash_evento(evento: dict, hash_anterior: str) -> str:
    return hash_conteudo({**evento, "hash_anterior": hash_anterior})


def registrar_auditoria(perfil: str, acao: str, recurso: str, resultado: str = "sucesso",
                        detalhes: dict | None = None) -> None:
    import json
    evento = {"data_hora": agora(), "perfil": perfil, "acao": acao, "recurso": recurso,
              "resultado": resultado,
              "detalhes_json": json.dumps(detalhes or {}, ensure_ascii=False, sort_keys=True, default=str)}
    # A trava garante que dois eventos simultâneos não apontem para o mesmo "anterior"
    with _trava_escrita, conectar() as con:
        ultimo = con.execute("SELECT hash_evento FROM auditoria ORDER BY id_evento DESC LIMIT 1").fetchone()
        hash_anterior = ultimo["hash_evento"] if ultimo else HASH_INICIAL
        con.execute(
            """INSERT INTO auditoria (data_hora, perfil, acao, recurso, resultado, detalhes_json, hash_anterior, hash_evento)
               VALUES (:data_hora, :perfil, :acao, :recurso, :resultado, :detalhes_json, :hash_anterior, :hash_evento)""",
            {**evento, "hash_anterior": hash_anterior, "hash_evento": _hash_evento(evento, hash_anterior)},
        )


def verificar_integridade_auditoria() -> dict:
    """Recalcula a corrente de hashes. Qualquer evento editado/apagado quebra a corrente."""
    with conectar() as con:
        eventos = con.execute("SELECT * FROM auditoria ORDER BY id_evento").fetchall()
    hash_esperado = HASH_INICIAL
    for ev in eventos:
        conteudo = {k: ev[k] for k in ("data_hora", "perfil", "acao", "recurso", "resultado", "detalhes_json")}
        if ev["hash_anterior"] != hash_esperado or ev["hash_evento"] != _hash_evento(conteudo, ev["hash_anterior"]):
            return {"integra": False, "eventos_verificados": len(eventos), "primeiro_evento_violado": ev["id_evento"]}
        hash_esperado = ev["hash_evento"]
    return {"integra": True, "eventos_verificados": len(eventos), "primeiro_evento_violado": None}


def verificar_integridade_leituras() -> dict:
    """Recalcula o hash de cada leitura e compara com o hash gravado na entrada."""
    df = consultar("SELECT * FROM leituras")
    violadas = [int(r["id_leitura"]) for r in df.to_dict("records")
                if hash_conteudo(conteudo_leitura(r)) != r["hash_leitura"]]
    return {"integra": not violadas, "leituras_verificadas": len(df), "leituras_violadas": violadas[:20]}
