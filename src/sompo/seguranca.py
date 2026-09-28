"""Segurança: autenticação por perfil, autorização, integridade e pseudonimização.

- Cada perfil de usuário tem sua própria chave (definida no .env, nunca no código).
  Em memória o sistema guarda apenas o hash SHA-256 das chaves e compara em
  tempo constante (evita ataque de temporização).
- Cada perfil só acessa o que precisa (princípio do menor privilégio).
- Dados pessoais (matrícula do operador) são pseudonimizados com HMAC antes de
  irem para o banco (LGPD: minimização de dados).
- Hashes SHA-256 garantem a integridade das leituras e da trilha de auditoria.
"""
import hashlib
import hmac
import json
from dataclasses import dataclass

from sompo import config


class AcessoNegado(Exception):
    """Chave ausente ou inválida (HTTP 401)."""


class PermissaoInsuficiente(Exception):
    """Perfil autenticado, mas sem permissão para a ação (HTTP 403)."""


@dataclass(frozen=True)
class Usuario:
    perfil: str
    permissoes: frozenset


def _sha256(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def autenticar(chave: str | None) -> Usuario:
    """Descobre o perfil dono da chave. Lança AcessoNegado se não houver."""
    if not chave or not chave.strip():
        raise AcessoNegado("Chave de acesso não informada.")
    hash_recebido = _sha256(chave.strip())
    perfil_encontrado = None
    # Percorre todos os perfis sempre (sem "break") para não vazar tempo de resposta
    for perfil, chave_valida in config.chaves_api().items():
        if hmac.compare_digest(hash_recebido, _sha256(chave_valida)):
            perfil_encontrado = perfil
    if perfil_encontrado is None:
        raise AcessoNegado("Chave de acesso inválida.")
    return Usuario(perfil_encontrado, frozenset(config.PERMISSOES[perfil_encontrado]))


def exigir_permissao(usuario: Usuario, permissao: str) -> None:
    if permissao not in usuario.permissoes:
        raise PermissaoInsuficiente(f"Perfil '{usuario.perfil}' não tem permissão '{permissao}'.")


def pseudonimizar(valor: str | None) -> str | None:
    """HMAC-SHA256 truncado: permite agrupar pelo operador sem guardar a matrícula."""
    if not valor:
        return None
    return "OP-" + hmac.new(config.segredo_pseudonimizacao().encode(), valor.strip().encode(), hashlib.sha256).hexdigest()[:12]


def hash_conteudo(conteudo: dict) -> str:
    """Hash canônico (chaves ordenadas) de um dicionário: mesma entrada -> mesmo hash."""
    return _sha256(json.dumps(conteudo, sort_keys=True, ensure_ascii=False, default=str))


def hash_arquivo(caminho) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(65536), b""):
            h.update(bloco)
    return h.hexdigest()
