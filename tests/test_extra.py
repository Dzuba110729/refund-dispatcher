"""Дополнительные проверки сверх 40 случаев: Telegram-бот и проверка текста модели."""
from refund_dispatcher.telegram import ManagerBot, parse_amount_kop

from .fakes import R, World, mk_order

MANAGER = 111


class FakeApi:
    def __init__(self):
        self.sent, self.alerts = [], []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        return len(self.sent)

    def answer_callback(self, cid, text="", alert=False):
        self.alerts.append((text, alert))


def cb(uid, data):
    return {"callback_query": {"id": "c1", "from": {"id": uid}, "data": data,
                               "message": {"chat": {"id": 5}, "message_id": 9}}}


def msg(uid, text):
    return {"message": {"from": {"id": uid}, "chat": {"id": 5}, "text": text}}


def test_stranger_cannot_press_buttons():
    w = World()
    w.orders.add(mk_order())
    t = w.submit()
    bot = ManagerBot(FakeApi(), w.dispatcher, {MANAGER})
    bot.handle_update(cb(999, f"a:{t}"))
    assert w.payments.total_kop == 0 and bot.api.alerts == [("Нет доступа", True)]


def test_manager_buttons_end_to_end():
    w = World()
    w.orders.add(mk_order())
    t = w.submit()
    bot = ManagerBot(FakeApi(), w.dispatcher, {MANAGER})
    bot.handle_update(cb(MANAGER, f"m:{t}"))
    bot.handle_update(msg(MANAGER, "mусор"))
    bot.handle_update(msg(MANAGER, "1000"))
    assert w.payments.total_kop == R(1000)


def test_model_reply_with_wrong_amount_replaced_by_template():
    w = World()
    w.orders.add(mk_order())
    t = w.submit()
    w.llm.reply_text = "Возврат одобрен на сумму 9 999 ₽"
    w.answer(t, "approve")
    assert "1 200 ₽" in w.customer.sent[-1][1] and "9 999" not in w.customer.sent[-1][1]


def test_parse_amount():
    assert parse_amount_kop("1 200 ₽") == 120000
    assert parse_amount_kop("1200,5") == 120050
    assert parse_amount_kop("abc") is None
