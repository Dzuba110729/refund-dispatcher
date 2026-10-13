from datetime import datetime, timedelta, timezone

from langgraph.checkpoint.memory import InMemorySaver

from refund_dispatcher.cards import fmt_rub
from refund_dispatcher.data import FakeOrders, FakePayments, Item, Order
from refund_dispatcher.graph import Deps, build_graph
from refund_dispatcher.llm import LlmError
from refund_dispatcher.models import Decision, JudgeDecision, ParsedRequest
from refund_dispatcher.service import Dispatcher
from refund_dispatcher.store import Store

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
EMAIL = "anna@example.com"


def R(rub: float) -> int:
    return round(rub * 100)


def P(**kw) -> ParsedRequest:
    base = dict(is_refund_request=True, order_id="1001", reason="changed_mind", items_mentioned=[],
                wants="refund", requested_amount_rub=None, language="ru", stop_flags=[],
                other_topics=[], summary="Клиент хочет вернуть товар")
    return ParsedRequest(**{**base, **kw})


def J(decision="approve", confidence=0.9, explanation="Похоже на брак") -> JudgeDecision:
    return JudgeDecision(decision=decision, confidence=confidence, explanation=explanation)


def item(name="Кроссовки", price=1200, **kw) -> Item:
    return Item(sku=name[:3], name=name, price_kop=R(price), **kw)


def mk_order(oid="1001", email=EMAIL, days=3, hours=0, items=None, status="delivered",
             paid=None, refunded=0) -> Order:
    items = items or [item()]
    total = sum(i.price_kop * i.qty for i in items)
    delivered = None if status in ("placed", "shipped") else NOW - timedelta(days=days, hours=hours)
    return Order(id=oid, customer_email=email, status=status, delivered_at=delivered,
                 paid_kop=R(paid) if paid is not None else total, refunded_kop=R(refunded),
                 payment_id=f"pay-{oid}", items=items)


class ScriptedLlm:
    """Модель-заглушка: отдаёт заранее заданные ответы. Настоящее поведение модели здесь НЕ проверяется."""

    def __init__(self):
        self.parsed, self.judge_result, self.reply_text = None, None, None

    def parse(self, text, attachments):
        if isinstance(self.parsed, Exception):
            raise self.parsed
        return self.parsed.model_copy(deep=True)

    def judge(self, case):
        if self.judge_result is None:
            raise LlmError("no judge scripted")
        return self.judge_result

    def reply(self, facts):
        if self.reply_text is not None:
            return self.reply_text
        base = f"[{facts['language']}] {facts['decision']}"
        if facts["amount_rub"] is not None:
            base += f" {fmt_rub(R(facts['amount_rub']))}"
        if facts["reason"]:
            base += f" {facts['reason']}"
        return base


class FakeManager:
    def __init__(self):
        self.cards, self.notes, self.reminders, self.escalations = [], [], [], []

    def send_card(self, ticket_id, text, can_approve):
        self.cards.append((ticket_id, text, can_approve))
        return len(self.cards)

    def send_note(self, text, reply_to=None):
        self.notes.append(text)

    def remind(self, ticket_id, text):
        self.reminders.append(ticket_id)

    def escalate(self, text):
        self.escalations.append(text)


class Outbox:
    def __init__(self):
        self.sent = []

    def send(self, email, text):
        self.sent.append((email, text))

    def forward(self, text):
        self.sent.append(text)


class World:
    def __init__(self, saver=None, store=None):
        self.now = NOW
        self.orders, self.payments = FakeOrders(), FakePayments()
        self.manager, self.customer, self.support = FakeManager(), Outbox(), Outbox()
        self.llm, self.slept = ScriptedLlm(), []
        self.store = store or Store()
        self.deps = Deps(orders=self.orders, payments=self.payments, llm=self.llm, manager=self.manager,
                         customer=self.customer, support=self.support, store=self.store,
                         clock=lambda: self.now, sleep=self.slept.append)
        self.graph = build_graph(self.deps, saver or InMemorySaver())
        self.dispatcher = Dispatcher(self.deps, self.graph)

    def submit(self, text="Хочу вернуть товар", parsed=None, judge=None, email=EMAIL, attachments=0):
        self.llm.parsed = parsed or P()
        self.llm.judge_result = judge
        return self.dispatcher.submit(text, email, attachments)

    def state(self, tid):
        return self.dispatcher.state(tid)

    def decision(self, tid) -> Decision:
        return Decision(**self.state(tid)["decision"])

    def answer(self, tid, action="approve", amount=None, reason=None):
        return self.dispatcher.answer(tid, action, manager_id=1, amount_kop=amount, reason=reason)
