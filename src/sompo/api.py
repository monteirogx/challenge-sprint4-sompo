"""API REST do MVP (FastAPI).

Execução: python main.py api   ->  documentação interativa em http://localhost:8000/docs
Autenticação: cabeçalho X-API-Key com a chave do perfil (ver .env.example).
"""
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

import pandas as pd
from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, ValidationError

from sompo import banco, config, modelo, relatorios, servico
from sompo.logger import obter_logger
from sompo.seguranca import AcessoNegado, PermissaoInsuficiente, Usuario, autenticar, exigir_permissao
from sompo.validacao import LeituraTelemetria

log = obter_logger("api")

@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    banco.inicializar_banco()
    try:
        modelo.carregar()
    except modelo.ModeloIndisponivel as erro:
        log.error("API iniciada SEM modelo: %s", erro)
    yield


app = FastAPI(
    lifespan=ciclo_de_vida,
    title="Sompo Seguros — Prevenção de Quebra de Maquinário (MVP Sprint 4)",
    version=config.VERSAO_SISTEMA,
    description="Recebe telemetria, calcula o score de risco de falha em 7 dias, gera alertas e relatórios. "
                "Autentique-se em **Authorize** com a chave do seu perfil.",
)
cabecalho_chave = APIKeyHeader(name="X-API-Key", auto_error=False)
Agrupamento = Literal["id_equipamento", "regiao", "tipo_equipamento", "tipo_operacao"]

EXEMPLO_LEITURA = {
    "id_equipamento": "EQ-007", "tipo_equipamento": "Colheitadeira", "regiao": "MT", "tipo_operacao": "Colheita",
    "idade_anos": 14, "horas_uso_continuo": 13.5, "rpm_medio": 2350, "temperatura_motor_c": 101.2,
    "pressao_oleo_psi": 24.0, "vibracao_mm_s": 8.4, "carga_motor_pct": 91, "temperatura_ambiente_c": 36.5,
    "umidade_relativa_pct": 38, "declividade_terreno_pct": 9, "operador_id": "MAT-12345",
}


# ==========================================
# MIDDLEWARE: rastreio de cada requisição
# ==========================================
@app.middleware("http")
async def registrar_requisicao(request: Request, call_next):
    id_requisicao = uuid.uuid4().hex[:12]
    request.state.id_requisicao = id_requisicao
    inicio = time.perf_counter()
    resposta = await call_next(request)
    resposta.headers["X-Request-ID"] = id_requisicao
    log.info("req=%s %s %s -> %s (%.0f ms)", id_requisicao, request.method, request.url.path,
             resposta.status_code, (time.perf_counter() - inicio) * 1000)
    return resposta


# ==========================================
# TRATAMENTO DE EXCEÇÕES (respostas padronizadas, nunca derruba a API)
# ==========================================
def _erro(status: int, codigo: str, mensagem: Any, request: Request) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "erro": codigo, "detalhe": mensagem, "id_requisicao": getattr(request.state, "id_requisicao", None)})


@app.exception_handler(AcessoNegado)
async def _acesso_negado(request: Request, erro: AcessoNegado):
    banco.registrar_auditoria("desconhecido", "autenticar", request.url.path, "negado", {"motivo": str(erro)})
    return _erro(401, "nao_autenticado", str(erro), request)


@app.exception_handler(PermissaoInsuficiente)
async def _sem_permissao(request: Request, erro: PermissaoInsuficiente):
    return _erro(403, "sem_permissao", str(erro), request)


@app.exception_handler(ValidationError)
async def _invalido(request: Request, erro: ValidationError):
    detalhes = [{"campo": ".".join(map(str, e["loc"])) or "leitura", "mensagem": e["msg"]} for e in erro.errors()]
    return _erro(422, "leitura_invalida", detalhes, request)


@app.exception_handler(banco.LeituraDuplicada)
async def _duplicada(request: Request, erro: banco.LeituraDuplicada):
    return _erro(409, "leitura_duplicada", str(erro), request)


@app.exception_handler(banco.AlertaNaoEncontrado)
async def _alerta_inexistente(request: Request, erro: banco.AlertaNaoEncontrado):
    return _erro(404, "alerta_nao_encontrado", str(erro), request)


@app.exception_handler(banco.TransicaoInvalida)
async def _transicao_invalida(request: Request, erro: banco.TransicaoInvalida):
    return _erro(409, "transicao_invalida", str(erro), request)


@app.exception_handler(modelo.ModeloIndisponivel)
async def _sem_modelo(request: Request, erro: modelo.ModeloIndisponivel):
    return _erro(503, "modelo_indisponivel", str(erro), request)


@app.exception_handler(Exception)
async def _inesperado(request: Request, erro: Exception):
    log.exception("Erro inesperado req=%s", getattr(request.state, "id_requisicao", None))
    return _erro(500, "erro_interno", "Erro inesperado; consulte o log pelo id_requisicao.", request)


# ==========================================
# CONTROLE DE ACESSO
# ==========================================
def usuario_atual(chave: str | None = Depends(cabecalho_chave)) -> Usuario:
    return autenticar(chave)


def permissao(nome: str):
    def _verificar(request: Request, usuario: Usuario = Depends(usuario_atual)) -> Usuario:
        try:
            exigir_permissao(usuario, nome)
        except PermissaoInsuficiente:
            banco.registrar_auditoria(usuario.perfil, nome, request.url.path, "negado", {"motivo": "sem_permissao"})
            raise
        return usuario
    return _verificar


def _registros(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    for col in df.select_dtypes(include=["datetime", "datetimetz"]).columns:
        df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")
    for col in ("fatores_json", "recomendacao"):
        if col in df.columns:
            df[col] = df[col].map(json.loads)
    return json.loads(df.to_json(orient="records", force_ascii=False))


# ==========================================
# ROTAS
# ==========================================
@app.get("/saude", tags=["Sistema"])
def saude():
    """Verificação de disponibilidade (sem autenticação)."""
    try:
        versao = modelo.carregar()["versao"]
    except modelo.ModeloIndisponivel:
        versao = None
    return {"status": "ok" if versao else "degradado", "versao_sistema": config.VERSAO_SISTEMA, "versao_modelo": versao}


@app.get("/contrato", tags=["Sistema"])
def contrato():
    """Esquema de dados aceito em /telemetria, com faixas válidas e critérios de risco."""
    return {"esquema_leitura": LeituraTelemetria.model_json_schema(),
            "niveis_risco": dict(config.NIVEIS_RISCO),
            "regras_seguranca": {k: f"{op} {v}" for k, (op, v, _) in config.REGRAS_SEGURANCA.items()},
            "limites_operacionais": {k: f"{op} {v}" for k, (op, v, _, _) in config.LIMITES_OPERACIONAIS.items()}}


@app.post("/telemetria", tags=["Telemetria"])
def enviar_telemetria(leitura: dict[str, Any] = Body(..., examples=[EXEMPLO_LEITURA]),
                      usuario: Usuario = Depends(permissao("enviar_telemetria"))):
    """Recebe uma leitura, grava, calcula o score e devolve nível, fatores e recomendações."""
    return servico.processar_leitura(leitura, usuario.perfil, origem="api")


@app.post("/telemetria/lote", tags=["Telemetria"])
def enviar_lote(leituras: list[dict[str, Any]] = Body(..., max_length=500),
                usuario: Usuario = Depends(permissao("enviar_telemetria"))):
    """Recebe até 500 leituras. Leituras inválidas são reportadas sem interromper as demais."""
    return servico.processar_lote(leituras, usuario.perfil, origem="api_lote")


class TratamentoAlerta(BaseModel):
    status: Literal["RECONHECIDO", "RESOLVIDO"]
    observacao: str | None = Field(default=None, max_length=500, description="Ex.: 'Rolamento trocado na OS 1234'")


@app.get("/alertas", tags=["Alertas"])
def listar_alertas(status: list[Literal["ABERTO", "RECONHECIDO", "RESOLVIDO"]] = Query(["ABERTO", "RECONHECIDO"]),
                   regiao: str | None = None, tipo_operacao: str | None = None,
                   usuario: Usuario = Depends(permissao("ver_alertas"))):
    """Alertas registrados (por padrão, os ativos: ABERTO e RECONHECIDO), do mais grave para o menos grave."""
    df = relatorios.alertas(tuple(status))
    if regiao:
        df = df[df["regiao"] == regiao.upper()]
    if tipo_operacao:
        df = df[df["tipo_operacao"] == tipo_operacao]
    banco.registrar_auditoria(usuario.perfil, "ver_alertas", "/alertas", "sucesso", {"retornados": len(df)})
    return {"total": len(df), "alertas": _registros(df)}


@app.patch("/alertas/{id_alerta}", tags=["Alertas"])
def tratar_alerta(id_alerta: int, tratamento: TratamentoAlerta, usuario: Usuario = Depends(permissao("tratar_alertas"))):
    """Técnico/gestor registra que o alerta foi reconhecido (inspeção agendada) ou resolvido (manutenção feita)."""
    resultado = banco.tratar_alerta(id_alerta, tratamento.status, usuario.perfil, tratamento.observacao)
    banco.registrar_auditoria(usuario.perfil, "tratar_alerta", f"alerta {id_alerta}", "sucesso",
                              {**resultado, "observacao": tratamento.observacao})
    return resultado


@app.get("/equipamentos/{id_equipamento}/historico", tags=["Alertas"])
def historico_equipamento(id_equipamento: str, limite: int = Query(30, ge=1, le=500),
                          usuario: Usuario = Depends(permissao("ver_alertas"))):
    df = banco.consultar("SELECT * FROM vw_risco WHERE id_equipamento = ? ORDER BY data_hora DESC LIMIT ?",
                         (id_equipamento.strip().upper(), limite))
    banco.registrar_auditoria(usuario.perfil, "ver_historico", id_equipamento, "sucesso", {"retornados": len(df)})
    return {"id_equipamento": id_equipamento.upper(), "leituras": _registros(df.drop(columns=["falha_7d"]))}


@app.get("/relatorios/visao-geral", tags=["Relatórios"])
def visao_geral(usuario: Usuario = Depends(permissao("ver_relatorios"))):
    return relatorios.visao_geral()


@app.get("/relatorios/resumo", tags=["Relatórios"])
def resumo(agrupar_por: Agrupamento = "regiao",
           usuario: Usuario = Depends(permissao("ver_relatorios"))):
    return _registros(relatorios.resumo_por(agrupar_por))


@app.get("/relatorios/tendencia", tags=["Relatórios"])
def tendencia(agrupar_por: Agrupamento | None = None,
              usuario: Usuario = Depends(permissao("ver_relatorios"))):
    return _registros(relatorios.tendencia(agrupar_por))


@app.get("/modelo", tags=["Modelo"])
def metricas_modelo(usuario: Usuario = Depends(permissao("ver_modelo"))):
    return modelo.carregar_metricas()


@app.get("/auditoria", tags=["Auditoria"])
def auditoria(limite: int = Query(50, ge=1, le=1000), usuario: Usuario = Depends(permissao("ver_auditoria"))):
    df = banco.consultar("SELECT * FROM auditoria ORDER BY id_evento DESC LIMIT ?", (limite,))
    return _registros(df)


@app.get("/auditoria/integridade", tags=["Auditoria"])
def integridade(usuario: Usuario = Depends(permissao("ver_auditoria"))):
    """Recalcula os hashes das leituras e da trilha de auditoria para detectar adulteração."""
    resultado = {"auditoria": banco.verificar_integridade_auditoria(),
                 "leituras": banco.verificar_integridade_leituras()}
    banco.registrar_auditoria(usuario.perfil, "verificar_integridade", "banco", "sucesso",
                              {k: v["integra"] for k, v in resultado.items()})
    return resultado
