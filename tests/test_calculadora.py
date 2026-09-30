"""A calculadora do Gemini: contas certas, e nada além de contas."""
import math

import pytest

from services.calculadora import ContaInvalida, calcula


@pytest.mark.parametrize(("expressao", "resultado"), [
    ("0.15*80", 12),
    ("2+3*4", 14),
    ("(2+3)*4", 20),
    ("2^10", 1024),  # o "^" de quem fala é potência
    ("7/2", 3.5),
    ("7//2", 3),
    ("10 % 3", 1),
    ("-5 + 2", -3),
    ("sqrt(144)", 12),
    ("raiz(2)*raiz(2)", 2),
    ("round(pi, 2)", 3.14),
    ("1,5 * 2", 3),  # vírgula decimal, do jeito brasileiro
    ("123456789 * 987654321", 121932631112635269),
])
def test_contas(expressao, resultado):
    assert calcula(expressao) == pytest.approx(resultado)


def test_inteiro_sai_sem_ponto_zero():
    assert isinstance(calcula("8/2"), int)
    assert calcula("log(e)") == 1


@pytest.mark.parametrize("expressao", [
    "__import__('os').system('dir')",
    "open('x')",
    "(1).__class__",
    "[1, 2]",
    "x + 1",
    "True + 1",
    "'a' * 3",
    "sqrt(x=4)",
    "",
    "1 +",
])
def test_nada_alem_de_contas(expressao):
    with pytest.raises(ContaInvalida):
        calcula(expressao)


@pytest.mark.parametrize("expressao", ["9**9**9", "10**1001", "1/0", "sqrt(-1)", "(-8)**0.5", "1e308*10", "1" * 201])
def test_contas_impossiveis_ou_grandes_demais_nao_travam(expressao):
    with pytest.raises(ContaInvalida):
        calcula(expressao)


def test_constantes():
    assert calcula("2*pi") == pytest.approx(2 * math.pi)
