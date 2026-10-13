"""Простые правила. Всё считает код, модель здесь не участвует."""
from datetime import datetime, timedelta

from .data import Item, Order, matches
from .models import ParsedRequest, RuleOutcome

RETURN_DAYS = 14             # обычный возврат
DEFECT_DAYS = 365            # брак с фото
AUTO_LIMIT_KOP = 5_000 * 100  # выше — решает человек
MAX_REFUNDS_90D = 3
ABUSE_WINDOW_DAYS = 90
MIN_CONFIDENCE = 0.8         # ниже — модель не считается

REASONS = {
    # код: (для менеджера, для клиента по-русски, для клиента по-английски)
    "ok": ("Все правила выполнены", "", ""),
    "already_refunded": ("Деньги по заказу уже возвращены", "Деньги по этому заказу уже возвращены.", "The money for this order has already been refunded."),
    "not_delivered": ("Заказ ещё не доставлен", "Заказ ещё не доставлен, вернуть его пока нельзя.", "The order has not been delivered yet, so it cannot be returned."),
    "cancelled_with_balance": ("Заказ отменён, но деньги вернулись не полностью", "", ""),
    "no_delivery_date": ("В заказе нет даты доставки", "", ""),
    "exchange_requested": ("Клиент хочет обмен, а не деньги", "", ""),
    "items_unmatched": ("Не удалось сопоставить товары из письма с заказом", "", ""),
    "key_activated": ("Цифровой ключ уже активирован", "Активированные ключи возврату не подлежат.", "Activated keys cannot be returned."),
    "hygiene_opened": ("Товар гигиенической категории вскрыт", "Вскрытые товары этой категории возврату не подлежат.", "Opened items of this category cannot be returned."),
    "made_to_order": ("Товар сделан под заказ, брака не доказано", "Товары, сделанные под заказ, возврату не подлежат.", "Made-to-order items cannot be returned."),
    "window_expired": ("Срок возврата истёк", "Срок возврата истёк.", "The return period has expired."),
    "reason_unclear": ("Причина возврата неясна, нужна оценка модели", "", ""),
    "partial_items": ("Часть товаров возврату не подлежит", "", ""),
    "amount_capped": ("Клиент просил больше, чем стоят товары; сумма ограничена", "", ""),
    "too_many_refunds": ("Больше 3 возвратов за 90 дней", "", ""),
    "amount_over_limit": ("Сумма выше 5 000 ₽", "", ""),
    "flag_legal": ("Клиент упоминает суд или надзорные органы", "", ""),
    "flag_injury": ("Клиент пишет о травме или вреде здоровью", "", ""),
    "flag_injection": ("В письме просьба игнорировать правила", "", ""),
    "not_owner": ("Заказ принадлежит другому клиенту", "Не удалось найти такой заказ на ваш адрес.", "We could not find this order for your address."),
    "orders_unavailable": ("База заказов не отвечает", "", ""),
    "judge_failed": ("Модель не смогла дать ответ", "", ""),
    "low_confidence": ("Модель не уверена в решении", "", ""),
    "model_handoff": ("Модель просит передать человеку", "", ""),
    "model_deny": ("Модель советует отказать", "", ""),
    "no_amount": ("Не удалось определить сумму", "", ""),
}


def select_items(order: Order, mentions: list[str]) -> list[Item] | None:
    if not mentions:
        return list(order.items)
    picked = [it for it in order.items if any(matches(m, it.name) for m in mentions)]
    return picked or None


def _deny(reasons: list[str], days: int | None = None) -> RuleOutcome:
    return RuleOutcome(verdict="deny", reasons=reasons, days=days)


def check_rules(req: ParsedRequest, order: Order, has_photo: bool,
                recent_refunds: int, now: datetime) -> RuleOutcome:
    if req.stop_flags:
        return RuleOutcome(verdict="stop", reasons=[f"flag_{f}" for f in req.stop_flags])

    refundable = max(order.paid_kop - order.refunded_kop, 0)
    if refundable == 0:
        return _deny(["already_refunded"])
    if order.status in ("placed", "shipped"):
        return _deny(["not_delivered"])
    if order.status == "cancelled":
        return RuleOutcome(verdict="stop", reasons=["cancelled_with_balance"])
    if order.delivered_at is None:
        return RuleOutcome(verdict="stop", reasons=["no_delivery_date"])
    if req.wants == "exchange":
        return RuleOutcome(verdict="stop", reasons=["exchange_requested"])

    days = (now - order.delivered_at).days
    defect_proven = req.reason == "defect" and has_photo

    items = select_items(order, req.items_mentioned)
    if items is None:
        return RuleOutcome(verdict="unclear", reasons=["items_unmatched"], days=days)

    allowed: list[Item] = []
    denied: list[str] = []
    for it in items:
        if it.activated:
            denied.append("key_activated")
        elif it.category == "hygiene" and it.opened:
            denied.append("hygiene_opened")
        elif it.category == "made_to_order" and not defect_proven:
            denied.append("made_to_order")
        else:
            allowed.append(it)
    if not allowed:
        return _deny(sorted(set(denied)), days)

    candidate = min(sum(i.price_kop * i.qty for i in allowed), refundable)
    risk = []
    if recent_refunds >= MAX_REFUNDS_90D:
        risk.append("too_many_refunds")
    if candidate > AUTO_LIMIT_KOP:
        risk.append("amount_over_limit")

    if defect_proven:
        limit = DEFECT_DAYS
    elif req.reason in ("wrong_size", "changed_mind"):
        limit = RETURN_DAYS
    else:
        limit = None

    if limit is None:
        if days > DEFECT_DAYS:
            return _deny(["window_expired"], days)
        return RuleOutcome(verdict="unclear", amount_kop=candidate,
                           reasons=["reason_unclear"], risk=risk, days=days)
    if days > limit:
        return _deny(["window_expired"], days)

    requested = round(req.requested_amount_rub * 100) if req.requested_amount_rub else None
    capped = requested is not None and requested > candidate
    reasons = ["ok"]
    if denied:
        reasons.append("partial_items")
    if capped:
        reasons.append("amount_capped")
    if risk:
        return RuleOutcome(verdict="stop", amount_kop=candidate, reasons=risk,
                           risk=risk, capped=capped, days=days)
    return RuleOutcome(verdict="approve", amount_kop=candidate, reasons=reasons,
                       capped=capped, days=days)
