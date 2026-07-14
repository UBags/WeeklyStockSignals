"""Test doubles for the GUI ports.

Replaces the old QMessageBox.warning monkeypatch — which reached into a
third-party class and, for the duration of the test, broke QMessageBox for
everything else in the process. A view now takes its Prompts, so a test hands it
one.

The recording is the point. The old stubs were `lambda *a, **k: None`: they made
the dialog go away but threw away what it said, so a test could not tell a view
that stayed quiet from one that popped a warning it did not expect. FakePrompts
keeps every message, so a test can assert on the text — and `assert not
prompts.warnings` is finally a meaningful thing to write.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Shown:
    """One message put in front of the user."""

    title: str
    text: str


@dataclass
class FakePrompts:
    """A Prompts that answers however the test says and remembers everything.

    Answers:
      confirm_answer   what confirm() returns. Default True — most tests are
                       about what happens AFTER the user says yes.
      save_path        what get_save_filename() returns. None = user cancelled.
      item             (label, accepted) from get_item(). Default = cancelled,
                       because a test that forgets to set it should NOT silently
                       proceed with an empty string.
      item_answers     for flows that ask more than once (add-filter asks for a
                       ticker, then for signals): answered in order, last one
                       repeating. Overrides `item` when set.
    """

    confirm_answer: bool = True
    save_path: str | None = None
    item: tuple[str, bool] = ("", False)
    item_answers: list[tuple[str, bool]] | None = None

    warnings: list[Shown] = field(default_factory=list)
    informations: list[Shown] = field(default_factory=list)
    criticals: list[Shown] = field(default_factory=list)
    confirms: list[Shown] = field(default_factory=list)
    confirm_defaults: list[bool] = field(default_factory=list)
    save_prompts: list[str] = field(default_factory=list)
    item_prompts: list[list[str]] = field(default_factory=list)

    # ---- Prompts ---- #

    def warning(self, parent, title: str, text: str) -> None:
        self.warnings.append(Shown(title, text))

    def information(self, parent, title: str, text: str) -> None:
        self.informations.append(Shown(title, text))

    def critical(self, parent, title: str, text: str) -> None:
        self.criticals.append(Shown(title, text))

    def confirm(
        self, parent, title: str, text: str, *, default_yes: bool = False,
    ) -> bool:
        self.confirms.append(Shown(title, text))
        self.confirm_defaults.append(default_yes)
        return self.confirm_answer

    def get_save_filename(
        self, parent, caption: str, suggested_path: str, file_filter: str,
    ) -> str | None:
        self.save_prompts.append(caption)
        return self.save_path

    def get_item(
        self, parent, title: str, label: str, items: list[str], *,
        current: int = 0, editable: bool = False,
    ) -> tuple[str, bool]:
        self.item_prompts.append(list(items))
        if self.item_answers:
            i = min(len(self.item_prompts) - 1, len(self.item_answers) - 1)
            return self.item_answers[i]
        return self.item

    # ---- assertions worth having ---- #

    @property
    def shown(self) -> list[Shown]:
        """Everything the user was shown, in no particular order."""
        return [*self.warnings, *self.informations, *self.criticals, *self.confirms]

    def texts(self) -> list[str]:
        return [s.text for s in self.shown]
