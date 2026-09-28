"""Integridade do banco, trilha de auditoria e controles de segurança."""
import sqlite3

import pytest

from conftest import leitura_exemplo
from sompo import banco, config
from sompo.seguranca import (AcessoNegado, PermissaoInsuficiente, autenticar, exigir_permissao, hash_conteudo,
                             pseudonimizar)


def _gravar(id_eq, data_hora):
    return banco.inserir_leitura(leitura_exemplo(id_eq, data_hora), origem="teste")


def test_leitura_duplicada_e_bloqueada():
    _gravar("EQ-801", "2026-08-01 08:00:00")
    with pytest.raises(banco.LeituraDuplicada):
        _gravar("EQ-801", "2026-08-01 08:00:00")


def test_banco_recusa_valor_fora_da_faixa_mesmo_via_sql_direto():
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        banco.inserir_leitura(leitura_exemplo("EQ-802", "2026-08-01 09:00:00", temperatura_motor_c=500), "teste")


def test_adulteracao_de_leitura_e_detectada():
    id_leitura = _gravar("EQ-803", "2026-08-01 10:00:00")
    assert banco.verificar_integridade_leituras()["integra"]
    with banco.conectar() as con:
        con.execute("UPDATE leituras SET temperatura_motor_c = 70 WHERE id_leitura = ?", (id_leitura,))
    resultado = banco.verificar_integridade_leituras()
    assert not resultado["integra"] and id_leitura in resultado["leituras_violadas"]
    with banco.conectar() as con:  # restaura para não afetar outros testes
        con.execute("UPDATE leituras SET temperatura_motor_c = 88 WHERE id_leitura = ?", (id_leitura,))
    assert banco.verificar_integridade_leituras()["integra"]


def test_trilha_de_auditoria_detecta_edicao_e_exclusao():
    for i in range(3):
        banco.registrar_auditoria("analista", "teste", f"recurso-{i}")
    assert banco.verificar_integridade_auditoria()["integra"]

    with banco.conectar() as con:
        ultimo = con.execute("SELECT id_evento, perfil FROM auditoria ORDER BY id_evento DESC LIMIT 1").fetchone()
        alvo = ultimo["id_evento"] - 1
        original = con.execute("SELECT perfil FROM auditoria WHERE id_evento = ?", (alvo,)).fetchone()["perfil"]
        con.execute("UPDATE auditoria SET perfil = 'admin' WHERE id_evento = ?", (alvo,))
    resultado = banco.verificar_integridade_auditoria()
    assert not resultado["integra"] and resultado["primeiro_evento_violado"] == alvo
    with banco.conectar() as con:
        con.execute("UPDATE auditoria SET perfil = ? WHERE id_evento = ?", (original, alvo))
    assert banco.verificar_integridade_auditoria()["integra"]

    with banco.conectar() as con:
        linha = dict(con.execute("SELECT * FROM auditoria WHERE id_evento = ?", (alvo,)).fetchone())
        con.execute("DELETE FROM auditoria WHERE id_evento = ?", (alvo,))
    assert not banco.verificar_integridade_auditoria()["integra"]
    with banco.conectar() as con:
        con.execute(f"INSERT INTO auditoria ({', '.join(linha)}) VALUES ({', '.join('?' * len(linha))})", tuple(linha.values()))
    assert banco.verificar_integridade_auditoria()["integra"]


def test_autenticacao_por_perfil():
    assert autenticar("chave-teste-gestor").perfil == "gestor"
    assert autenticar("  chave-teste-operador  ").perfil == "operador"
    for chave in ("", None, "chave-errada", "chave-teste-gestor-x"):
        with pytest.raises(AcessoNegado):
            autenticar(chave)


@pytest.mark.parametrize("perfil,permissao,permitido", [
    ("operador", "enviar_telemetria", True), ("operador", "ver_auditoria", False), ("operador", "ver_relatorios", False),
    ("gestor", "enviar_telemetria", False), ("gestor", "ver_relatorios", True),
    ("analista", "ver_auditoria", True), ("analista", "enviar_telemetria", False),
    ("tecnico", "ver_modelo", False), ("admin", "ver_auditoria", True),
])
def test_matriz_de_permissoes(perfil, permissao, permitido):
    usuario = autenticar(f"chave-teste-{perfil}")
    if permitido:
        exigir_permissao(usuario, permissao)
    else:
        with pytest.raises(PermissaoInsuficiente):
            exigir_permissao(usuario, permissao)


def test_pseudonimizacao_e_deterministica_e_nao_reversivel():
    p1, p2 = pseudonimizar("MAT-123"), pseudonimizar("MAT-123")
    assert p1 == p2 and p1.startswith("OP-") and "123" not in p1
    assert pseudonimizar("MAT-124") != p1
    assert pseudonimizar(None) is None


def test_hash_canonico_independe_da_ordem_das_chaves():
    assert hash_conteudo({"a": 1, "b": 2}) == hash_conteudo({"b": 2, "a": 1})
    assert hash_conteudo({"a": 1}) != hash_conteudo({"a": 1.5})


def test_nenhuma_chave_de_api_no_codigo_fonte():
    for arquivo in (config.RAIZ / "src").rglob("*.py"):
        texto = arquivo.read_text(encoding="utf-8")
        for chave in config.chaves_api().values():
            assert chave not in texto, f"chave encontrada em {arquivo}"
