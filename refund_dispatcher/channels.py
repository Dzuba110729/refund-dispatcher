"""Каналы связи. Менеджер — Telegram (telegram.py). Клиент и поддержка — пока заглушка в консоли."""
from typing import Protocol


class ManagerChannel(Protocol):
    def send_card(self, ticket_id: str, text: str, can_approve: bool) -> int: ...
    def send_note(self, text: str, reply_to: int | None = None) -> None: ...
    def remind(self, ticket_id: str, text: str) -> None: ...
    def escalate(self, text: str) -> None: ...


class CustomerChannel(Protocol):
    def send(self, email: str, text: str) -> None: ...


class SupportChannel(Protocol):
    def forward(self, text: str) -> None: ...


class ConsoleChannel:
    """Печатает вместо отправки. Почта/SMS клиенту не реализованы."""

    def send(self, email: str, text: str) -> None:
        print(f"→ клиенту {email}: {text}")

    def forward(self, text: str) -> None:
        print(f"→ в поддержку: {text}")
