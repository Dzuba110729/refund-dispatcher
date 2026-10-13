"""Заказы и платежи. Здесь только заглушки (Fake*): настоящие системы подключаются через Protocol."""
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel


class Item(BaseModel):
    sku: str
    name: str
    price_kop: int
    qty: int = 1
    category: Literal["regular", "hygiene", "digital_key", "made_to_order"] = "regular"
    opened: bool = False
    activated: bool = False


class Order(BaseModel):
    id: str
    customer_email: str
    status: Literal["placed", "shipped", "delivered", "cancelled"]
    delivered_at: datetime | None = None
    paid_kop: int
    refunded_kop: int = 0
    payment_id: str
    items: list[Item]


class RefundResult(BaseModel):
    refund_id: str
    amount_kop: int


class OrdersUnavailable(Exception):
    pass


class OrdersRepo(Protocol):
    def get(self, order_id: str) -> Order | None: ...
    def by_customer(self, email: str) -> list[Order]: ...
    def recent_refunds(self, email: str, since: datetime) -> int: ...


class PaymentGateway(Protocol):
    def refund(self, payment_id: str, amount_kop: int, idempotency_key: str) -> RefundResult: ...


_OCR = str.maketrans({"O": "0", "o": "0", "О": "0", "о": "0", "l": "1", "I": "1"})


def normalize_order_id(raw: str) -> str:
    """'#1O01' -> '1001'. Буквы меняем только если в итоге получились одни цифры."""
    s = raw.strip().lstrip("#№").strip()
    t = s.translate(_OCR)
    return t if t.isdigit() else s


def matches(mention: str, name: str) -> bool:
    a, b = mention.lower().strip(), name.lower().strip()
    return bool(a) and (a in b or b in a)


class FakeOrders:
    def __init__(self, orders=(), fail_times: int = 0):
        self.by_id = {o.id: o for o in orders}
        self.history: list[tuple[str, datetime]] = []
        self.fail_times = fail_times

    def add(self, order: Order) -> None:
        self.by_id[order.id] = order

    def add_refund_history(self, email: str, count: int, at: datetime) -> None:
        self.history += [(email.lower(), at)] * count

    def _maybe_fail(self) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OrdersUnavailable("orders db down")

    def get(self, order_id: str) -> Order | None:
        self._maybe_fail()
        return self.by_id.get(order_id)

    def by_customer(self, email: str) -> list[Order]:
        self._maybe_fail()
        return [o for o in self.by_id.values() if o.customer_email.lower() == email.lower()]

    def recent_refunds(self, email: str, since: datetime) -> int:
        self._maybe_fail()
        return sum(1 for e, at in self.history if e == email.lower() and at >= since)


class FakePayments:
    def __init__(self):
        self.done: dict[str, RefundResult] = {}

    def refund(self, payment_id: str, amount_kop: int, idempotency_key: str) -> RefundResult:
        if amount_kop <= 0:
            raise ValueError("amount must be positive")
        if idempotency_key not in self.done:
            self.done[idempotency_key] = RefundResult(
                refund_id=f"rf-{len(self.done) + 1}", amount_kop=amount_kop)
        return self.done[idempotency_key]

    @property
    def total_kop(self) -> int:
        return sum(r.amount_kop for r in self.done.values())
