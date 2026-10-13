"""Граф LangGraph: разбор → поиск заказа → правила → (модель) → решение → пауза в Telegram → возврат → ответ."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .cards import ASK_ORDER, render_card, template_reply, verify_reply
from .channels import CustomerChannel, ManagerChannel, SupportChannel
from .data import (OrdersRepo, OrdersUnavailable, Order, PaymentGateway,
                   matches, normalize_order_id)
from .llm import Llm, LlmError
from .models import Decision, JudgeDecision, ParsedRequest, RuleOutcome
from .rules import ABUSE_WINDOW_DAYS, AUTO_LIMIT_KOP, MIN_CONFIDENCE, REASONS, check_rules
from .store import Store

INJECTION_RE = re.compile(
    r"(игнорир\w*|забудь|ignore|disregard|forget)\W+"
    r"(все\W+|всё\W+|предыдущ\w+\W+|previous\W+|all\W+|your\W+|the\W+)*"
    r"(правил\w*|инструкц\w*|rules?|instructions?)", re.I)


@dataclass
class Deps:
    orders: OrdersRepo
    payments: PaymentGateway
    llm: Llm
    manager: ManagerChannel
    customer: CustomerChannel
    support: SupportChannel
    store: Store
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]


class State(TypedDict, total=False):
    ticket_id: str
    text: str
    email: str
    attachments: int
    parsed: dict | None
    lookup: str
    order: dict | None
    order_id: str | None
    rules: dict | None
    judge: dict | None
    decision: dict
    card_message_id: int
    merged_into: str
    manager: dict
    final: dict
    refund: dict
    reply: str
    outcome: str


def refundable_kop(order: dict) -> int:
    return max(order["paid_kop"] - order["refunded_kop"], 0)


def public_reasons(codes: list[str], lang: str) -> str:
    idx = 2 if lang == "en" else 1
    return " ".join(REASONS[c][idx] for c in codes if c in REASONS and REASONS[c][idx])


def validate_answer(values: dict, answer: dict) -> dict:
    """Проверяет ответ менеджера. Бросает ValueError с понятным текстом."""
    action = answer.get("action")
    if action not in ("approve", "deny"):
        raise ValueError("Неизвестное действие")
    d = values["decision"]
    order = values.get("order")
    amount = 0
    reason = answer.get("reason")
    if action == "approve":
        if not order:
            raise ValueError("Заказ не найден — одобрить возврат нельзя, решите вручную.")
        amount = answer.get("amount_kop") or d["amount_kop"]
        if amount <= 0:
            raise ValueError("Нужна сумма возврата.")
        if amount > refundable_kop(order):
            raise ValueError("Сумма больше, чем можно вернуть по заказу.")
    return {"action": action, "amount_kop": amount, "reason": reason,
            "manager_id": answer.get("manager_id")}


def build_graph(deps: Deps, checkpointer):
    now = deps.clock

    def audit(s: State, event: str, data: dict) -> None:
        deps.store.audit(s["ticket_id"], event, data, now())

    # 1. Разбор письма (модель)
    def parse_message(s: State):
        try:
            p = deps.llm.parse(s["text"], s.get("attachments", 0))
        except LlmError as e:
            audit(s, "parse_failed", {"error": str(e)})
            return {"parsed": None, "outcome": "parse_failed"}
        if INJECTION_RE.search(s["text"]) and "injection" not in p.stop_flags:
            p.stop_flags.append("injection")
        audit(s, "parsed", p.model_dump())
        return {"parsed": p.model_dump()}

    # 2. Поиск заказа (код)
    def find_order(s: State):
        p = ParsedRequest(**s["parsed"])
        oid = normalize_order_id(p.order_id) if p.order_id else None
        for attempt in range(3):
            try:
                return _lookup(s, p, oid)
            except OrdersUnavailable:
                if attempt < 2:
                    deps.sleep(2 ** attempt)
        audit(s, "orders_unavailable", {})
        return {"lookup": "unavailable", "order": None, "order_id": oid}

    def _lookup(s: State, p: ParsedRequest, oid: str | None):
        email = s["email"].lower()
        if oid:
            order = deps.orders.get(oid)
            if order is None:
                return {"lookup": "missing", "order": None, "order_id": oid}
            if order.customer_email.lower() != email:
                return {"lookup": "not_owner", "order": None, "order_id": oid}
            return {"lookup": "found", "order": order.model_dump(mode="json"), "order_id": oid}
        cands = deps.orders.by_customer(email)
        if len(cands) > 1 and p.items_mentioned:
            cands = [o for o in cands if any(matches(m, i.name) for m in p.items_mentioned for i in o.items)]
        if len(cands) == 1:
            return {"lookup": "found", "order": cands[0].model_dump(mode="json"), "order_id": cands[0].id}
        return {"lookup": "ambiguous" if cands else "missing", "order": None, "order_id": None}

    # 3. Правила (код)
    def check_rules_node(s: State):
        p = ParsedRequest(**s["parsed"])
        order = Order(**s["order"])
        since = now() - timedelta(days=ABUSE_WINDOW_DAYS)
        try:
            recent = deps.orders.recent_refunds(s["email"], since)
        except OrdersUnavailable:
            return {"lookup": "unavailable", "rules": None}
        r = check_rules(p, order, s.get("attachments", 0) > 0, recent, now())
        audit(s, "rules", r.model_dump())
        return {"rules": r.model_dump()}

    # 4. Модель оценивает неясное
    def judge(s: State):
        order, r = s["order"], s["rules"]
        case = {
            "request": s["parsed"],
            "customer_message": s["text"],
            "order": {"items": [{"name": i["name"], "price_rub": i["price_kop"] / 100, "category": i["category"]}
                                for i in order["items"]],
                      "days_since_delivery": r["days"], "paid_rub": order["paid_kop"] / 100},
            "why_rules_could_not_decide": [REASONS[c][0] for c in r["reasons"]],
        }
        try:
            j = deps.llm.judge(case)
        except LlmError as e:
            audit(s, "judge_failed", {"error": str(e)})
            return {"judge": None}
        audit(s, "judge", j.model_dump())
        return {"judge": j.model_dump()}

    # 5. Решение (код): модель не может обойти ограничители
    def decide(s: State):
        p = ParsedRequest(**s["parsed"])
        urgent = "injury" in p.stop_flags
        lookup = s.get("lookup")
        if lookup == "not_owner":
            d = Decision(action="deny", reasons=["not_owner"])
        elif lookup == "unavailable":
            d = Decision(action="handoff", reasons=["orders_unavailable"], source="system", suggest=False)
        else:
            r = RuleOutcome(**s["rules"])
            if r.verdict == "approve":
                d = Decision(action="approve", amount_kop=r.amount_kop, reasons=r.reasons)
            elif r.verdict == "deny":
                d = Decision(action="deny", reasons=r.reasons)
            elif r.verdict == "stop":
                d = Decision(action="handoff", amount_kop=r.amount_kop, reasons=r.reasons, suggest=False)
            else:
                d = _from_judge(s, r)
        d.urgent = urgent
        audit(s, "decision", d.model_dump())
        return {"decision": d.model_dump()}

    def _from_judge(s: State, r: RuleOutcome) -> Decision:
        if s.get("judge") is None:
            return Decision(action="handoff", reasons=["judge_failed"], source="system", suggest=False)
        j = JudgeDecision(**s["judge"])
        base = dict(source="model", note=j.explanation)
        if j.confidence < MIN_CONFIDENCE:
            return Decision(action="handoff", reasons=["low_confidence"], suggest=False, **base)
        if j.decision == "handoff":
            return Decision(action="handoff", reasons=["model_handoff"], suggest=False, **base)
        if j.decision == "deny":
            return Decision(action="deny", reasons=["model_deny"], **base)
        if r.risk:
            return Decision(action="handoff", reasons=r.risk, suggest=False, **base)
        if r.amount_kop <= 0 or r.amount_kop > AUTO_LIMIT_KOP:
            return Decision(action="handoff", reasons=["no_amount"], suggest=False, **base)
        return Decision(action="approve", amount_kop=r.amount_kop, reasons=["ok"], **base)

    # 6a. Карточка менеджеру. Отдельный узел от паузы: при возобновлении карточка не уходит второй раз.
    def notify_manager(s: State):
        order_id = (s.get("order") or {}).get("id") or s.get("order_id")
        existing = deps.store.find_open_by_order(order_id) if order_id else None
        if existing and existing != s["ticket_id"]:
            deps.manager.send_note(f"Дополнение к заявке #{existing[:8]} от {s['email']}: «{s['text'][:500]}»")
            audit(s, "merged", {"into": existing})
            return {"merged_into": existing, "outcome": "merged"}
        text, can_approve = render_card(s)
        msg_id = deps.manager.send_card(s["ticket_id"], text, can_approve)
        deps.store.open_ticket(s["ticket_id"], order_id, msg_id, now())
        return {"card_message_id": msg_id}

    # 6b. Пауза. Состояние лежит в базе, перезапуск сервера её не ломает.
    def await_manager(s: State):
        answer = interrupt({"ticket_id": s["ticket_id"]})
        final = validate_answer(s, answer)
        audit(s, "manager_answer", final)
        return {"manager": answer, "final": final}

    # 7. Деньги уходят один раз: ключ идемпотентности = номер заявки
    def execute_refund(s: State):
        f = s["final"]
        if f["action"] != "approve" or s.get("refund"):
            return {}
        res = deps.payments.refund(s["order"]["payment_id"], f["amount_kop"], idempotency_key=s["ticket_id"])
        audit(s, "refunded", res.model_dump())
        return {"refund": res.model_dump()}

    # 8. Ответ клиенту: пишет модель, проверяет код
    def reply_customer(s: State):
        f = s["final"]
        lang = (s["parsed"] or {}).get("language", "ru")
        if f["action"] == "deny":
            public = f.get("reason") or public_reasons(s["decision"]["reasons"], lang)
        else:
            public = ""
        facts = {"decision": "одобрено" if f["action"] == "approve" else "отказано", "language": lang,
                 "amount_rub": f["amount_kop"] / 100 if f["action"] == "approve" else None,
                 "reason": public or None}
        try:
            text = deps.llm.reply(facts)
        except LlmError:
            text = None
        if text is None or not verify_reply(text, f):
            text = template_reply(f, public, lang)
        deps.customer.send(s["email"], text)
        return {"reply": text, "outcome": "refunded" if f["action"] == "approve" else "denied"}

    def ask_customer(s: State):
        lang = (s["parsed"] or {}).get("language", "ru")
        deps.customer.send(s["email"], ASK_ORDER[lang])
        return {"outcome": "asked_customer"}

    def support_handoff(s: State):
        p = s.get("parsed") or {}
        why = p.get("summary") or "письмо не удалось разобрать"
        deps.support.forward(f"Письмо от {s['email']} не про возврат: {why}\n«{s['text'][:500]}»")
        return {"outcome": "support_handoff"}

    # 9. Аудит
    def audit_log(s: State):
        for topic in (s.get("parsed") or {}).get("other_topics", []):
            if s.get("outcome") in ("refunded", "denied"):
                deps.support.forward(f"Дополнительная тема от {s['email']}: {topic}")
        audit(s, "finished", {"outcome": s.get("outcome"), "final": s.get("final")})
        deps.store.close(s["ticket_id"])
        return {}

    g = StateGraph(State)
    for name, fn in [("parse_message", parse_message), ("find_order", find_order),
                     ("check_rules", check_rules_node), ("judge", judge), ("decide", decide),
                     ("notify_manager", notify_manager), ("await_manager", await_manager),
                     ("execute_refund", execute_refund), ("reply_customer", reply_customer),
                     ("ask_customer", ask_customer), ("support_handoff", support_handoff),
                     ("audit_log", audit_log)]:
        g.add_node(name, fn)

    g.add_edge(START, "parse_message")
    g.add_conditional_edges("parse_message",
                            lambda s: "find_order" if s.get("parsed") and s["parsed"]["is_refund_request"] else "support_handoff",
                            ["find_order", "support_handoff"])
    g.add_conditional_edges("find_order", lambda s: {"found": "check_rules", "missing": "ask_customer",
                                                    "ambiguous": "ask_customer"}.get(s["lookup"], "decide"),
                            ["check_rules", "ask_customer", "decide"])
    g.add_conditional_edges("check_rules",
                            lambda s: "judge" if s.get("rules") and s["rules"]["verdict"] == "unclear" else "decide",
                            ["judge", "decide"])
    g.add_edge("judge", "decide")
    g.add_edge("decide", "notify_manager")
    g.add_conditional_edges("notify_manager", lambda s: "audit_log" if s.get("merged_into") else "await_manager",
                            ["await_manager", "audit_log"])
    g.add_edge("await_manager", "execute_refund")
    g.add_edge("execute_refund", "reply_customer")
    g.add_edge("reply_customer", "audit_log")
    g.add_edge("ask_customer", "audit_log")
    g.add_edge("support_handoff", "audit_log")
    g.add_edge("audit_log", END)
    return g.compile(checkpointer=checkpointer)
