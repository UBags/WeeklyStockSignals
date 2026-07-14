"""The prompts a view can put in front of the user, as a collaborator.

The views called Qt's dialog statics directly:

    QMessageBox.warning(self, "Save failed", message)
    confirm = QMessageBox.question(
        self, "Delete subscriber?", "...",
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
    )
    if confirm != QMessageBox.Yes:
        return

Two problems, and the test suite only ever complained about the first.

1. A static is a global. The only way to test a view without a human clicking
   things was to rewrite QMessageBox's class attributes for the duration —
   twenty-two monkeypatches across seven files, each one temporarily breaking
   QMessageBox for anything else running in the same process.

2. The confirmation contract leaked Qt into the view's control flow. Every
   destructive action ended up asking "is this Qt enum equal to that Qt enum",
   which is a lot of surface area for a question whose real answer is yes or no.

`Prompts` is the port. `confirm()` returns a bool — the view asks a question and
gets an answer, rather than a button identity it has to decode. `QtPrompts` is
the one production implementation; views take `prompts: Prompts = DEFAULT_PROMPTS`,
so the wiring in main_window does not change at all.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QWidget


@runtime_checkable
class Prompts(Protocol):
    """What a view needs in order to talk to the user."""

    def warning(self, parent: QWidget | None, title: str, text: str) -> None:
        """Something went wrong, but the app is fine."""

    def information(self, parent: QWidget | None, title: str, text: str) -> None:
        """Something worked. Acknowledge and move on."""

    def critical(self, parent: QWidget | None, title: str, text: str) -> None:
        """Something went badly wrong."""

    def confirm(
        self,
        parent: QWidget | None,
        title: str,
        text: str,
        *,
        default_yes: bool = False,
    ) -> bool:
        """Ask a yes/no question. True means the user said yes.

        `default_yes` picks the pre-selected button — False for anything
        destructive, so that a stray Enter keypress cannot delete a subscriber
        or cancel a running backfill."""

    def get_save_filename(
        self,
        parent: QWidget | None,
        caption: str,
        suggested_path: str,
        file_filter: str,
    ) -> str | None:
        """Ask where to save. None means the user cancelled."""

    def get_item(
        self,
        parent: QWidget | None,
        title: str,
        label: str,
        items: list[str],
        *,
        current: int = 0,
        editable: bool = False,
    ) -> tuple[str, bool]:
        """Pick one of `items`. Returns (choice, accepted); when accepted is
        False the choice is meaningless and the caller must bail."""


class QtPrompts:
    """The real thing. A thin, deliberately dumb adapter over Qt's statics —
    there is no logic here to get wrong, which is the point: everything worth
    testing now lives in the views, on the far side of the Prompts seam."""

    def warning(self, parent: QWidget | None, title: str, text: str) -> None:
        QMessageBox.warning(parent, title, text)

    def information(self, parent: QWidget | None, title: str, text: str) -> None:
        QMessageBox.information(parent, title, text)

    def critical(self, parent: QWidget | None, title: str, text: str) -> None:
        QMessageBox.critical(parent, title, text)

    def confirm(
        self,
        parent: QWidget | None,
        title: str,
        text: str,
        *,
        default_yes: bool = False,
    ) -> bool:
        Yes = QMessageBox.StandardButton.Yes
        No = QMessageBox.StandardButton.No
        reply = QMessageBox.question(
            parent, title, text, Yes | No, Yes if default_yes else No,
        )
        return reply == Yes

    def get_save_filename(
        self,
        parent: QWidget | None,
        caption: str,
        suggested_path: str,
        file_filter: str,
    ) -> str | None:
        path_str, _ = QFileDialog.getSaveFileName(
            parent, caption, suggested_path, file_filter,
        )
        return path_str or None

    def get_item(
        self,
        parent: QWidget | None,
        title: str,
        label: str,
        items: list[str],
        *,
        current: int = 0,
        editable: bool = False,
    ) -> tuple[str, bool]:
        return QInputDialog.getItem(
            parent, title, label, items, current, editable,
        )


DEFAULT_PROMPTS: Prompts = QtPrompts()
"""The default every view takes, so main_window's wiring is unchanged."""
