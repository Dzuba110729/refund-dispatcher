"""Тексты: карточка для менеджера и ответ клиенту."""
import html
import re

from .rules import REASONS


def fmt_rub(kop: int) -> str:
    rub, k = divmod(kop, 100)
    s = f"{rub:,}".replace(",", " ")
    return f"{s},{k:02d} ₽" if k else f"{s} ₽"


def reason_text(codes: list[str]) -> str:
    return "; ".join(REASONS[c][0] for c in codes if c in REASONS and c != "ok") or REASONS["ok"][0]


def render_card(s: dict) -> tuple[str, bool]:
    """Возвращает (HTML-текст, можно ли одобрять). Одобрять нельзя, если заказа нет."""
    e = html.escape
    d = s["decision"]
    order = s.get("order")
    p = s.get("parsed") or {}
    lines = []
    head = f"Заявка на возврат #{s['ticket_id'][:8]}"
    lines.append(("🚨 СРОЧНО · " if d.get("urgent") else "") + head)
    lines.append(f"Клиент: {e(s['email'])}")
    if order:
        days = (s.get("rules") or {}).get("days")
        when = f" · доставлен {days} дн. назад" if days is not None else ""
        lines.append(f"Заказ {e(order['id'])}{when} · оплачено {fmt_rub(order['paid_kop'])}")
        lines.append("Товары: " + e(", ".join(i["name"] for i in order["items"])))
    elif s.get("order_id"):
        lines.append(f"Заказ: {e(str(s['order_id']))} (данные недоступны)")
    if p.get("summary"):
        lines.append(f"Суть: {e(p['summary'])}")
    lines.append(f"Письмо: «{e(s['text'][:500])}»")
    lines.append("")
    if d["suggest"]:
        verb = {"approve": f"ОДОБРИТЬ {fmt_rub(d['amount_kop'])}", "deny": "ОТКАЗАТЬ"}[d["action"]]
        lines.append(f"Предложение агента ({'правила' if d['source'] == 'rules' else 'модель'}): {verb}")
    else:
        lines.append("Предложения нет — решите сами")
    lines.append("Почему: " + e(reason_text(d["reasons"])))
    if d.get("note"):
        lines.append("Заметка модели: " + e(d["note"]))
    return "\n".join(lines), order is not None


def verify_reply(text: str, final: dict) -> bool:
    """Код проверяет текст модели: при одобрении в нём должна быть настоящая сумма."""
    if final["action"] != "approve":
        return True
    compact = re.sub(r"[\s  ]", "", text)
    rub, k = divmod(final["amount_kop"], 100)
    wanted = [f"{rub},{k:02d}", f"{rub}.{k:02d}"] if k else [str(rub)]
    return any(w in compact for w in wanted)


def template_reply(final: dict, public_reason: str, lang: str) -> str:
    if lang == "en":
        if final["action"] == "approve":
            return f"Your refund of {fmt_rub(final['amount_kop'])} has been approved."
        return "We are sorry, but we cannot approve this refund. " + public_reason
    if final["action"] == "approve":
        return f"Ваш возврат на сумму {fmt_rub(final['amount_kop'])} одобрен."
    return "К сожалению, мы не можем одобрить возврат. " + public_reason


ASK_ORDER = {
    "ru": "Здравствуйте! Не получилось найти ваш заказ. Пришлите, пожалуйста, номер заказа.",
    "en": "Hello! We could not find your order. Please send us the order number.",
}
