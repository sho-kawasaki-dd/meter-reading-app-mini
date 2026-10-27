"""業務数値を丸めずに計算する Decimal 演算。

演算ごとに必要な桁数を求めた局所的な Context を使い、グローバルな精度には依存しない。
加減算の小数桁数は両辺の最大、乗算の小数桁数は両辺の和になる。
"""

from __future__ import annotations

from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)


def _plain(value: Decimal) -> Decimal:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("Decimal は有限値で指定してください。")
    if exponent > 0:
        return Decimal(format(value, "f"))
    return value


def _scale(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("Decimal は有限値で指定してください。")
    return -exponent


def _integer_digits(value: Decimal) -> int:
    return max(value.adjusted() + 1, 1)


def _exact_context(precision: int) -> Context:
    return Context(
        prec=precision,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        traps=[InvalidOperation, DivisionByZero, Overflow, Inexact],
    )


def _additive_context(left: Decimal, right: Decimal) -> Context:
    integer_digits = max(_integer_digits(left), _integer_digits(right))
    return _exact_context(integer_digits + max(_scale(left), _scale(right)) + 1)


def exact_add(left: Decimal, right: Decimal) -> Decimal:
    """丸めなしで加算する。"""

    left, right = _plain(left), _plain(right)
    return _additive_context(left, right).add(left, right)


def exact_subtract(left: Decimal, right: Decimal) -> Decimal:
    """丸めなしで減算する。"""

    left, right = _plain(left), _plain(right)
    return _additive_context(left, right).subtract(left, right)


def exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    """丸めなしで乗算する。"""

    left, right = _plain(left), _plain(right)
    precision = _integer_digits(left) + _integer_digits(right) + _scale(left) + _scale(right)
    return _exact_context(precision).multiply(left, right)


__all__ = ["exact_add", "exact_multiply", "exact_subtract"]
