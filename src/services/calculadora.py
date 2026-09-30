"""Contas de verdade para o Gemini: "quanto é 15% de 80?", "raiz de 2 vezes 7".

O modelo erra conta de cabeça (principalmente com muitos dígitos). A expressão
vem do Gemini, então nada de eval: só números, operadores e umas funções,
avaliados pela árvore do ast — e com teto no tamanho, para ninguém travar o
bot com 9**9**9.
"""
from __future__ import annotations

import ast
import math
import operator
import re

_MAX_CARACTERES = 200
_MAX_EXPOENTE = 1000
_MAX_ABS = 1e300  # além disso o float vira inf e a resposta não diz nada

_BINARIOS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARIOS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCOES = {
    "sqrt": math.sqrt, "raiz": math.sqrt, "abs": abs, "round": round,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "exp": math.exp,
    "floor": math.floor, "ceil": math.ceil,
}
_CONSTANTES = {"pi": math.pi, "e": math.e}


class ContaInvalida(ValueError):
    pass


def calcula(expressao: str) -> float | int:
    """Avalia uma expressão aritmética; ContaInvalida se não for só conta."""
    # "1,5" é decimal (vírgula colada nos dígitos); "round(pi, 2)" separa argumentos
    expressao = re.sub(r"(\d),(\d)", r"\1.\2", expressao.strip().replace("^", "**"))
    if not expressao:
        raise ContaInvalida("expressão vazia")
    if len(expressao) > _MAX_CARACTERES:
        raise ContaInvalida("expressão grande demais")
    try:
        arvore = ast.parse(expressao, mode="eval")
    except SyntaxError as erro:
        raise ContaInvalida(f"não entendi a conta: {expressao}") from erro
    resultado = _avalia(arvore.body)
    if isinstance(resultado, float) and resultado.is_integer() and abs(resultado) < 2**53:
        return int(resultado)  # "8" e não "8.0" na fala
    return resultado


def _avalia(no: ast.AST) -> float | int:
    if isinstance(no, ast.Constant) and isinstance(no.value, (int, float)) and not isinstance(no.value, bool):
        return no.value
    if isinstance(no, ast.Name) and no.id in _CONSTANTES:
        return _CONSTANTES[no.id]
    if isinstance(no, ast.UnaryOp) and type(no.op) in _UNARIOS:
        return _UNARIOS[type(no.op)](_avalia(no.operand))
    if isinstance(no, ast.BinOp) and type(no.op) in _BINARIOS:
        esquerda, direita = _avalia(no.left), _avalia(no.right)
        if isinstance(no.op, ast.Pow) and abs(direita) > _MAX_EXPOENTE:
            raise ContaInvalida("expoente grande demais")
        try:
            resultado = _BINARIOS[type(no.op)](esquerda, direita)
        except ZeroDivisionError as erro:
            raise ContaInvalida("divisão por zero") from erro
        except OverflowError as erro:
            raise ContaInvalida("número grande demais") from erro
        return _confere(resultado)
    if (
        isinstance(no, ast.Call) and isinstance(no.func, ast.Name) and no.func.id in _FUNCOES
        and not no.keywords and 1 <= len(no.args) <= 2
    ):
        try:
            return _confere(_FUNCOES[no.func.id](*(_avalia(arg) for arg in no.args)))
        except (ValueError, TypeError, OverflowError) as erro:
            raise ContaInvalida(f"conta impossível: {erro}") from erro
    raise ContaInvalida("só dá para fazer contas com números")


def _confere(valor):
    if isinstance(valor, complex):
        raise ContaInvalida("o resultado não é um número real")
    if abs(valor) > _MAX_ABS:
        raise ContaInvalida("número grande demais")
    return valor
