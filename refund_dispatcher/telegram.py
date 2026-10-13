"""Telegram: отправка карточек, кнопки, разбор ответов менеджера. Кнопки принимает только список разрешённых пользователей."""
import logging
import re

import httpx

from .cards import fmt_rub
from .service import Dispatcher

log = logging.getLogger(__name__)


class TelegramApi:
    """Тонкая обёртка над Bot API. Токен лежит в адресе запросов — логи httpx выше WARNING не включать."""

    def __init__(self, token: str):
        self.http = httpx.Client(base_url=f"https://api.telegram.org/bot{token}/", timeout=40.0)

    def _call(self, method: str, **params):
        r = self.http.post(method, json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> int:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self._call("sendMessage", **params)["message_id"]

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)

    def clear_buttons(self, chat_id: int, message_id: int) -> None:
        self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup={"inline_keyboard": []})

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return self._call("getUpdates", offset=offset, timeout=timeout,
                          allowed_updates=["message", "callback_query"])


class TelegramChannel:
    """ManagerChannel поверх Telegram."""

    def __init__(self, api: TelegramApi, chat_id: int, escalation_chat_id: int | None = None):
        self.api, self.chat_id = api, chat_id
        self.escalation_chat_id = escalation_chat_id or chat_id

    def send_card(self, ticket_id: str, text: str, can_approve: bool) -> int:
        rows = []
        if can_approve:
            rows.append([{"text": "✅ Одобрить", "callback_data": f"a:{ticket_id}"},
                         {"text": "❌ Отказать", "callback_data": f"d:{ticket_id}"}])
            rows.append([{"text": "✏️ Другая сумма", "callback_data": f"m:{ticket_id}"}])
        else:
            rows.append([{"text": "❌ Отказать", "callback_data": f"d:{ticket_id}"}])
        return self.api.send_message(self.chat_id, text, {"inline_keyboard": rows})

    def send_note(self, text: str, reply_to: int | None = None) -> None:
        self.api.send_message(self.chat_id, text)

    def remind(self, ticket_id: str, text: str) -> None:
        self.api.send_message(self.chat_id, "⏰ " + text)

    def escalate(self, text: str) -> None:
        self.api.send_message(self.escalation_chat_id, "‼️ " + text)


def parse_amount_kop(text: str) -> int | None:
    m = re.fullmatch(r"\s*(\d[\d\s]*)(?:[.,](\d{1,2}))?\s*(?:₽|р|руб\.?)?\s*", text)
    if not m:
        return None
    rub = int(re.sub(r"\s", "", m.group(1)))
    k = int((m.group(2) or "0").ljust(2, "0"))
    return rub * 100 + k


class ManagerBot:
    def __init__(self, api: TelegramApi, dispatcher: Dispatcher, allowed_user_ids: set[int]):
        self.api, self.dispatcher, self.allowed = api, dispatcher, allowed_user_ids
        self.awaiting: dict[int, tuple[str, str]] = {}  # user_id -> (что ждём, ticket_id)

    def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            self._on_callback(u["callback_query"])
        elif "message" in u:
            self._on_message(u["message"])

    def _on_callback(self, cb: dict) -> None:
        uid = cb["from"]["id"]
        if uid not in self.allowed:
            log.warning("callback from unauthorized user %s", uid)
            self.api.answer_callback(cb["id"], "Нет доступа", alert=True)
            return
        kind, _, ticket_id = cb["data"].partition(":")
        chat_id = cb["message"]["chat"]["id"]
        values = self.dispatcher.state(ticket_id)
        if not values:
            self.api.answer_callback(cb["id"], "Заявка не найдена", alert=True)
            return
        self.api.answer_callback(cb["id"])
        if kind == "a":
            amount = values["decision"]["amount_kop"]
            if amount > 0:
                self._finish(chat_id, uid, ticket_id, "approve")
            else:
                self.awaiting[uid] = ("amount", ticket_id)
                self.api.send_message(chat_id, "Напишите сумму возврата в рублях, например 1200 или 1200,50.")
        elif kind == "m":
            self.awaiting[uid] = ("amount", ticket_id)
            self.api.send_message(chat_id, "Напишите сумму возврата в рублях, например 1200 или 1200,50.")
        elif kind == "d":
            self.awaiting[uid] = ("deny_reason", ticket_id)
            self.api.send_message(chat_id, "Напишите причину отказа для клиента. Знак «-» оставит стандартную причину.")

    def _on_message(self, msg: dict) -> None:
        uid = msg["from"]["id"]
        if uid not in self.allowed or uid not in self.awaiting:
            return
        chat_id, text = msg["chat"]["id"], msg.get("text", "")
        kind, ticket_id = self.awaiting[uid]
        if kind == "amount":
            kop = parse_amount_kop(text)
            if kop is None:
                self.api.send_message(chat_id, "Не понял сумму. Пример: 1200 или 1200,50.")
                return
            del self.awaiting[uid]
            self._finish(chat_id, uid, ticket_id, "approve", amount_kop=kop)
        else:
            del self.awaiting[uid]
            reason = None if text.strip() == "-" else text.strip()
            self._finish(chat_id, uid, ticket_id, "deny", reason=reason)

    def _finish(self, chat_id: int, uid: int, ticket_id: str, action: str,
                amount_kop: int | None = None, reason: str | None = None) -> None:
        res = self.dispatcher.answer(ticket_id, action, uid, amount_kop, reason)
        if res.status == "done":
            what = "Возврат одобрен" if action == "approve" else "Отказ отправлен"
            self.api.send_message(chat_id, f"✅ {what} (заявка #{ticket_id[:8]}).")
        else:
            self.api.send_message(chat_id, f"⚠️ {res.message}")


def run_forever(bot: ManagerBot, api: TelegramApi, dispatcher: Dispatcher) -> None:
    offset = None
    while True:
        for u in api.get_updates(offset):
            offset = u["update_id"] + 1
            try:
                bot.handle_update(u)
            except Exception:
                log.exception("update failed")
        dispatcher.sweep()
