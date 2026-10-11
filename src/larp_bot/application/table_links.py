from __future__ import annotations

from dataclasses import dataclass

from larp_bot.domain.models import Event

YANDEX_UPLOAD_FAILED = "[НЕ УДАЛОСЬ ЗАГРУЗИТЬ НА ЯНДЕКС ДИСК]"


@dataclass(frozen=True)
class TableProviderLinks:
    google: str | None
    yandex: str | None

    def for_player(self) -> str:
        if self.google is None and self.yandex is not None:
            return self.yandex
        lines: list[str] = []
        if self.google is not None:
            lines.append(f"Google Drive: {self.google}")
        if self.yandex is not None:
            lines.append(f"Яндекс Диск: {self.yandex}")
        if not lines:
            raise RuntimeError("table has no public link")
        return "\n".join(lines)

    def for_admin(self) -> str:
        text = self.for_player()
        if self.google is not None and self.yandex is None:
            return f"{text}\n{YANDEX_UPLOAD_FAILED}"
        return text


def table_provider_links(google_url: str | None, stored_url: str | None) -> TableProviderLinks:
    yandex_url = stored_url if stored_url != google_url else None
    return TableProviderLinks(google=google_url, yandex=yandex_url)


def master_table_links(event: Event) -> TableProviderLinks:
    return table_provider_links(event.master_table_google_url, event.public_registration_url)


def public_table_links(event: Event) -> TableProviderLinks:
    return table_provider_links(event.public_table_google_url, event.public_table_public_url)


def pass_table_links(event: Event) -> TableProviderLinks:
    return table_provider_links(event.pass_table_google_url, event.pass_table_public_url)
