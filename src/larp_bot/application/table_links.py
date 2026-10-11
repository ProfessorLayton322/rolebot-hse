from __future__ import annotations

from dataclasses import dataclass

from larp_bot.domain.models import Event


@dataclass(frozen=True)
class TableProviderLinks:
    google: str | None
    yandex: str | None

    def for_player(self) -> str:
        if self.google is not None:
            links = [f"Google Drive: {self.google}"]
            if self.yandex is not None:
                links.append(f"Яндекс Диск: {self.yandex}")
            return "\n".join(links)
        if self.yandex is not None:
            return self.yandex
        raise RuntimeError("table has no public link")

    def for_admin(self) -> str:
        return self.for_player()


def table_provider_links(google_url: str | None, stored_url: str | None) -> TableProviderLinks:
    yandex_url = stored_url if stored_url != google_url else None
    return TableProviderLinks(google=google_url, yandex=yandex_url)


def master_table_links(event: Event) -> TableProviderLinks:
    return table_provider_links(event.master_table_google_url, event.public_registration_url)


def public_table_links(event: Event) -> TableProviderLinks:
    return table_provider_links(event.public_table_google_url, event.public_table_public_url)


def pass_table_links(event: Event) -> TableProviderLinks:
    return table_provider_links(event.pass_table_google_url, event.pass_table_public_url)


def google_event_tables_ready(event: Event) -> bool:
    return all(
        (
            event.master_table_google_url,
            event.public_table_google_url,
            event.pass_table_google_url,
        )
    )
