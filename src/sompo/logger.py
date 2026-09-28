"""Logs técnicos da aplicação (arquivo rotativo + console).

Os logs técnicos registram o funcionamento do sistema (erros, tempos, etapas
do pipeline). A trilha de auditoria de negócio (quem fez o quê) fica no banco,
na tabela `auditoria`, com encadeamento de hashes (ver banco.py).
"""
import logging
from logging.handlers import RotatingFileHandler

from sompo import config

_FORMATO = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_configurado = False


def obter_logger(nome: str) -> logging.Logger:
    global _configurado
    if not _configurado:
        raiz = logging.getLogger("sompo")
        raiz.setLevel(logging.INFO)
        try:
            config.DIR_LOGS.mkdir(parents=True, exist_ok=True)
            arquivo = RotatingFileHandler(
                config.DIR_LOGS / "sompo.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
            )
            arquivo.setFormatter(logging.Formatter(_FORMATO))
            raiz.addHandler(arquivo)
        except OSError:
            pass  # sem permissão de escrita: segue só com o console
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_FORMATO))
        raiz.addHandler(console)
        _configurado = True
    return logging.getLogger(f"sompo.{nome}")
