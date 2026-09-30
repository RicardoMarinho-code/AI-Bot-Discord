"""Anotações pedidas por voz ("Jarvis, anota: comprar pão").

Por pessoa e por servidor, guardadas em data/notas.json: anotação que some
num reinício não serve para nada.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

_ARQUIVO = os.path.join("data", "notas.json")
MAX_NOTAS = 50  # por pessoa: é bloco de notas, não banco de dados
MAX_CARACTERES = 300

_notas: dict[str, list[str]] | None = None  # "guild:user" → notas; lido na 1ª vez


def _chave(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def _todas() -> dict[str, list[str]]:
    global _notas
    if _notas is None:
        try:
            with open(_ARQUIVO, encoding="utf-8") as f:
                lidas = json.load(f)
            _notas = {str(k): [str(n) for n in v] for k, v in lidas.items()}
        except FileNotFoundError:
            _notas = {}
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("📝 o arquivo de notas está estragado — começando vazio", exc_info=True)
            _notas = {}
    return _notas


def _salva() -> None:
    try:
        os.makedirs(os.path.dirname(_ARQUIVO), exist_ok=True)
        temporario = _ARQUIVO + ".tmp"
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(_todas(), f, ensure_ascii=False)
        os.replace(temporario, _ARQUIVO)
    except OSError:
        log.warning("📝 não consegui salvar as notas", exc_info=True)


def anota(guild_id: int, user_id: int, texto: str) -> int:
    """Guarda uma nota; devolve quantas a pessoa tem. ValueError se vazia ou cheia."""
    texto = " ".join(texto.split())[:MAX_CARACTERES]
    if not texto:
        raise ValueError("nota vazia")
    notas = _todas().setdefault(_chave(guild_id, user_id), [])
    if len(notas) >= MAX_NOTAS:
        raise ValueError(f"já são {MAX_NOTAS} notas: apague algumas antes")
    notas.append(texto)
    _salva()
    return len(notas)


def de(guild_id: int, user_id: int) -> list[str]:
    return list(_todas().get(_chave(guild_id, user_id), []))


def apaga(guild_id: int, user_id: int) -> int:
    """Apaga todas as notas da pessoa; devolve quantas eram."""
    notas = _todas().pop(_chave(guild_id, user_id), [])
    if notas:
        _salva()
    return len(notas)
