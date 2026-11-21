#!/usr/bin/env python3
# Copyleft 🄯 — all rights reversed 2026 https://github.com/postsonde
# SPDX-License-Identifier: GPL-3.0-or-later
"""goabase-tui: Terminal user interface for the goabase party API."""

__author__ = "<postsonde>"
__version__ = "1.0.0"


import json
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, Static

API = "https://www.goabase.net/api/party/json/?limit=300"
FAVORITES_FILE = Path.home() / ".goabase_tui_favorites.json"
DEFAULT_THEME = "dracula"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) goabase-tui/1.0",
    "Accept": "application/json",
}

STATUS_STYLES = {
    "Scheduled": "green",
    "Postponed": "yellow",
    "Moved": "cyan",
    "Cancelled": "red",
}


def fetch_parties() -> list[dict]:
    """Download the party list from the goabase API."""
    req = urllib.request.Request(API, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read().decode("utf-8").strip()
        if not raw:
            raise ValueError("Empty response from Goabase API")
        return json.loads(raw).get("partylist", [])


def fetch_party_detail(url: str) -> dict:
    """Download full details (line-up, description, location) for a single party."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read().decode("utf-8").strip()
        if not raw:
            raise ValueError("Empty detail response from Goabase API")
        data = json.loads(raw)
        return data.get("party", data)


def parse_dt(value: str, reference: datetime) -> datetime:
    """Parse an API timestamp. Naive values are interpreted in the reference timezone."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=reference.tzinfo)
    return dt


def load_favorites() -> set[int]:
    """Load saved favorite party IDs from disk."""
    try:
        if FAVORITES_FILE.exists():
            return set(json.loads(FAVORITES_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set()


def save_favorites(favs: set[int]) -> None:
    """Persist favorite party IDs to disk."""
    try:
        FAVORITES_FILE.write_text(json.dumps(sorted(favs)), encoding="utf-8")
    except Exception:
        pass


class GoabaseTUI(App):
    CSS = """
    Screen { background: $background; color: $text; }
    #filter { border: tall $primary; }
    #filter:focus { border: tall $accent; }
    #main { height: 1fr; }
    #table-box { width: 60%; }
    DataTable { height: 1fr; }
    #detail-scroll { width: 40%; border: round $primary; padding: 1 2; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Reload"),
        Binding("t", "toggle_view", "Today/Upcoming/All"),
        Binding("f", "toggle_favorite", "Fav ★"),
        Binding("m", "toggle_only_favorites", "Only Favs"),
        Binding("enter", "load_party_details", "Line-up/Info"),
        Binding("i", "open_flyer", "Flyer"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("escape", "focus_table", "Back to list"),
        Binding("o", "open_link", "Open link"),
        Binding("y", "copy_link", "Copy link"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.parties: list[dict] = []
        self.filtered: list[dict] = []
        self.detail_cache: dict[int, dict] = {}
        self.favorites: set[int] = load_favorites()
        self.mode = "today"
        self.only_favorites = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Filter: party, town, country, type, status …", id="filter")
        with Horizontal(id="main"):
            with Vertical(id="table-box"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=False, cell_padding=1)
            with VerticalScroll(id="detail-scroll"):
                yield Static("Loading parties …", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "goabase"
        self.theme = DEFAULT_THEME
        table = self.query_one(DataTable)
        table.add_column("★", width=2)
        table.add_column("Date", width=10)
        table.add_column("Time", width=5)
        table.add_column("Place", width=18)
        table.add_column("Party", width=30)
        table.add_column("Type", width=13)
        table.add_column("Status", width=10)
        self.load_data()

    @work(thread=True, exclusive=True, group="list")
    def load_data(self) -> None:
        try:
            data = fetch_parties()
        except Exception as exc:
            self.call_from_thread(self.notify, f"Could not load parties: {exc}", severity="error")
            return
        self.call_from_thread(self.set_parties, data)

    @work(thread=True, exclusive=True, group="detail")
    def fetch_details_worker(self, party_id: int, json_url: str) -> None:
        try:
            extra = fetch_party_detail(json_url)
        except Exception as exc:
            self.call_from_thread(self.notify, f"Could not load details: {exc}", severity="error")
            return
        self.call_from_thread(self.on_details_loaded, party_id, extra)

    def on_details_loaded(self, party_id: int, extra: dict) -> None:
        self.detail_cache[party_id] = extra
        idx = self.current_index()
        if idx is not None and self.filtered[idx].get("id") == party_id:
            self.show_detail(idx)

    def set_parties(self, data: list[dict]) -> None:
        self.parties = data
        self.apply_filter(self.query_one("#filter", Input).value)

    def apply_filter(self, text: str) -> None:
        now = datetime.now().astimezone()
        today = now.date().isoformat()
        words = text.lower().split()

        def in_view(p: dict) -> bool:
            if self.mode == "all":
                return True
            date_start = p.get("dateStart") or ""
            if self.mode == "upcoming":
                return date_start[:10] > today
            if date_start[:10] == today:
                return True
            try:
                start = parse_dt(date_start, now)
                end = parse_dt(p.get("dateEnd") or "", now)
            except (ValueError, KeyError):
                return False
            return start <= now <= end

        def matches(p: dict) -> bool:
            if self.only_favorites and p.get("id") not in self.favorites:
                return False
            haystack = " ".join(
                [
                    p.get("nameParty") or "",
                    p.get("nameTown") or "",
                    p.get("nameCountry") or "",
                    p.get("isoCountry") or "",
                    p.get("nameType") or "",
                    p.get("nameStatus") or "",
                ]
            ).lower()
            return all(word in haystack for word in words)

        self.filtered = [p for p in self.parties if in_view(p) and matches(p)]

        view_labels = {"today": "Today", "upcoming": "Upcoming", "all": "All"}
        parts = [f"{view_labels[self.mode]}: {len(self.filtered)} parties"]
        if self.only_favorites:
            parts.append("★ Favorites")
        self.sub_title = " · ".join(parts)

        table = self.query_one(DataTable)
        table.clear()
        for p in self.filtered:
            pid = p["id"]
            star = Text("★", style="bold yellow") if pid in self.favorites else Text(" ")
            status = p.get("nameStatus") or ""
            status_style = STATUS_STYLES.get(status, "")
            table.add_row(
                star,
                (p.get("dateStart") or "")[:10],
                (p.get("dateStart") or "")[11:16],
                f"{p.get('nameTown') or ''} ({p.get('isoCountry') or ''})",
                (p.get("nameParty") or "")[:30],
                p.get("nameType") or "",
                Text(status, style=status_style) if status_style else Text(status),
                key=str(pid),
            )
        self.show_detail(0 if self.filtered else None)

    def show_detail(self, index: int | None, loading_extra: bool = False) -> None:
        detail = self.query_one("#detail", Static)
        if index is None or index >= len(self.filtered):
            detail.update("No matches.")
            return
        p = self.filtered[index]
        pid = p["id"]
        star_prefix = "★ " if pid in self.favorites else ""
        status = p.get("nameStatus") or ""
        status_style = STATUS_STYLES.get(status, "")
        status_text = f"[{status_style}]{escape(status)}[/]" if status_style else escape(status)
        url = p.get("urlPartyHtml") or ""
        flyer_url = p.get("urlImageLarge") or p.get("urlImageFull") or ""

        lines = [
            f"[bold magenta]{star_prefix}🎵 {escape(p.get('nameParty') or '')}[/]\n",
            f"[cyan]📅[/] {(p.get('dateStart') or '')[:16]} to {(p.get('dateEnd') or '')[:16]}",
            f"[cyan]📍[/] {escape(p.get('nameTown') or '')}, {escape(p.get('nameCountry') or '')}",
            f"[cyan]🎧[/] {escape(p.get('nameType') or '')} — {status_text}",
            f"[cyan]🏷️[/]  Organizer: {escape(p.get('nameOrganizer') or '')}",
        ]

        if flyer_url:
            lines.append("[cyan]🖼️[/]  Flyer available (press [bold]i[/bold] to open)")

        extra = self.detail_cache.get(pid)
        if extra:
            memo_location = (extra.get("memoLocation") or "").strip()
            lineup = (extra.get("textLineUp") or extra.get("memoLineUp") or "").strip()
            info = (extra.get("textInfo") or extra.get("memoInfo") or "").strip()
            styles = (extra.get("nameStyles") or "").strip()
            if styles:
                lines.append(f"[cyan]🎶[/] Styles: {escape(styles)}")
            if memo_location:
                lines.append(f"\n[bold cyan]🗺️ Location[/]\n{escape(memo_location)}")
            if lineup:
                lines.append(f"\n[bold cyan]🎛️ Line-up[/]\n{escape(lineup)}")
            if info:
                lines.append(f"\n[bold cyan]ℹ️ Info[/]\n{escape(info)}")
        elif loading_extra:
            lines.append("\n[italic yellow]⏳ Loading line-up & details …[/]")
        else:
            lines.append("\n[dim]Press Enter to load full line-up & info[/]")

        if url:
            lines.append(f"\n🔗 [underline]{escape(url)}[/underline]")
        lines.append("\n[dim]Enter: details · i: flyer · f: fav · o: open · y: copy[/]")
        detail.update("\n".join(lines))

    def current_index(self) -> int | None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        if row is None or not 0 <= row < len(self.filtered):
            return None
        return row

    def current_party(self) -> dict | None:
        idx = self.current_index()
        if idx is None:
            return None
        return self.filtered[idx]

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(DataTable).focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_detail(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_load_party_details()

    def action_refresh(self) -> None:
        self.sub_title = "loading …"
        self.detail_cache.clear()
        self.load_data()

    def action_toggle_view(self) -> None:
        order = ["today", "upcoming", "all"]
        self.mode = order[(order.index(self.mode) + 1) % len(order)]
        self.apply_filter(self.query_one("#filter", Input).value)

    def action_toggle_favorite(self) -> None:
        idx = self.current_index()
        if idx is None:
            return
        p = self.filtered[idx]
        pid = p["id"]
        if pid in self.favorites:
            self.favorites.remove(pid)
            self.notify(f"Removed ★ {p.get('nameParty', '')}")
        else:
            self.favorites.add(pid)
            self.notify(f"Saved ★ {p.get('nameParty', '')}")
        save_favorites(self.favorites)
        self.apply_filter(self.query_one("#filter", Input).value)
        table = self.query_one(DataTable)
        if idx < len(self.filtered):
            table.move_cursor(row=idx)

    def action_toggle_only_favorites(self) -> None:
        self.only_favorites = not self.only_favorites
        self.apply_filter(self.query_one("#filter", Input).value)

    def action_load_party_details(self) -> None:
        idx = self.current_index()
        if idx is None:
            return
        p = self.filtered[idx]
        pid = p["id"]
        if pid in self.detail_cache:
            self.show_detail(idx)
            return
        json_url = p.get("urlPartyJson")
        if not json_url:
            self.notify("No detail URL available", severity="warning")
            return
        self.show_detail(idx, loading_extra=True)
        self.fetch_details_worker(pid, json_url)

    def action_open_flyer(self) -> None:
        p = self.current_party()
        if not p:
            return
        flyer_url = p.get("urlImageLarge") or p.get("urlImageFull") or p.get("urlImageMedium")
        if flyer_url:
            webbrowser.open(flyer_url)
            self.notify("Opened flyer in browser")
        else:
            self.notify("No flyer available for this party", severity="warning")

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one(DataTable).focus()

    def action_open_link(self) -> None:
        p = self.current_party()
        if p and p.get("urlPartyHtml"):
            webbrowser.open(p["urlPartyHtml"])
            self.notify("Opened link in browser")

    def action_copy_link(self) -> None:
        p = self.current_party()
        if p and p.get("urlPartyHtml"):
            self.copy_to_clipboard(p["urlPartyHtml"])
            self.notify("Link copied")


def main() -> None:
    GoabaseTUI().run()


if __name__ == "__main__":
    main()
