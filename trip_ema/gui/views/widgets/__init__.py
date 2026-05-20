"""Reusable GUI widgets (Phase 15c).

This subpackage contains small, view-agnostic widgets that get
composed into the top-level tab views. Keeping them isolated here
makes them unit-testable in their own right (separately from
whatever tab is currently consuming them) and avoids the picker-
duplication that was creeping into the codebase as more tabs
needed exchange awareness.
"""
