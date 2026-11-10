from decimal import Decimal, InvalidOperation


def to_cents(value: str | float | Decimal) -> int:
    """Legacy conversion only; new facts always contain integer cents."""
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError("Amount must be nonnegative with at most two decimal places")
        return int(amount * 100)
    except InvalidOperation as exc:
        raise ValueError("Invalid amount") from exc
