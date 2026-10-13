"""Точка входа для приложения: принять письмо, принять ответ менеджера, напомнить о зависших заявках."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from langgraph.types import Command

from .graph import Deps, validate_answer

REMIND_AFTER = timedelta(hours=24)
ESCALATE_AFTER = timedelta(hours=24)  # после напоминания


@dataclass
class AnswerResult:
    status: str  # done | already_processed | rejected
    message: str


class Dispatcher:
    def __init__(self, deps: Deps, graph):
        self.deps, self.graph, self.store = deps, graph, deps.store

    @staticmethod
    def _cfg(ticket_id: str) -> dict:
        return {"configurable": {"thread_id": ticket_id}}

    def submit(self, text: str, email: str, attachments: int = 0) -> str:
        ticket_id = uuid.uuid4().hex
        self.graph.invoke({"ticket_id": ticket_id, "text": text, "email": email, "attachments": attachments},
                          self._cfg(ticket_id))
        return ticket_id

    def state(self, ticket_id: str) -> dict:
        return dict(self.graph.get_state(self._cfg(ticket_id)).values)

    def answer(self, ticket_id: str, action: str, manager_id: int,
               amount_kop: int | None = None, reason: str | None = None) -> AnswerResult:
        cfg = self._cfg(ticket_id)
        snap = self.graph.get_state(cfg)
        if not snap.values:
            return AnswerResult("rejected", "Заявка не найдена.")
        payload = {"action": action, "amount_kop": amount_kop, "reason": reason, "manager_id": manager_id}
        try:
            validate_answer(snap.values, payload)
        except ValueError as e:
            return AnswerResult("rejected", str(e))
        if not self.store.claim(ticket_id):   # защита от двойного нажатия
            return AnswerResult("already_processed", "Заявка уже обработана.")
        try:
            if snap.next == ("await_manager",):
                self.graph.invoke(Command(resume=payload), cfg)
            elif snap.next:                   # прошлая попытка упала после паузы (например, платёжка) — продолжаем
                self.graph.invoke(None, cfg)
        except Exception:
            self.store.release(ticket_id)
            raise
        return AnswerResult("done", "Готово.")

    def sweep(self) -> None:
        """Напоминание через 24 ч без ответа, ещё через 24 ч — сообщение в общий канал. Заявка не теряется."""
        now = self.deps.clock()
        for t in self.store.pending():
            notified = datetime.fromisoformat(t["notified_at"])
            if t["reminded_at"] is None:
                if now - notified >= REMIND_AFTER:
                    self.deps.manager.remind(t["ticket_id"], f"Заявка #{t['ticket_id'][:8]} ждёт решения уже сутки.")
                    self.store.mark_reminded(t["ticket_id"], now)
            elif t["escalated_at"] is None:
                if now - datetime.fromisoformat(t["reminded_at"]) >= ESCALATE_AFTER:
                    self.deps.manager.escalate(f"Заявка #{t['ticket_id'][:8]} без ответа больше двух суток.")
                    self.store.mark_escalated(t["ticket_id"], now)
