"""Deterministic selectors for fixtures and already accounted application choices."""

from __future__ import annotations

from .contracts import Answer, ProviderFailure, Selection


class FakeSelector:
    model = "fixture.v1"

    def __init__(self, selections=(), *, cost_micros=0):
        self.selections = iter(selections)
        self.cost_micros = cost_micros
        self.calls = []
        self.receipts = {}

    def maximum_charge_micros(self, objective, observation, decisions):
        return self.cost_micros

    def select(self, selection_id, objective, observation, decisions, *, cancel, deadline):
        if cancel.is_set():
            raise ProviderFailure("cancelled", submitted=False, uncharged=True)
        self.calls.append(selection_id)
        selection = next(self.selections, None)
        if selection is None:
            answers = tuple(
                Answer(
                    question_id=q.id,
                    value=(
                        q.options[0].id if q.kind == "choice" else False if q.kind == "noul" else q.minimum
                    ),
                )
                for q in decisions.questions
            )
            selection = Selection(answers=answers, model=self.model, cost_micros=self.cost_micros)
        if isinstance(selection, Exception):
            raise selection
        self.receipts[selection_id] = selection
        return selection

    def lookup(self, attempt_id):
        return self.receipts.get(attempt_id)


class RecordedSelector:
    """Reuse an authorized application selection. The accounting link is mandatory.

    Charges already settled by the application are mirrored with zero new cost.
    Its exact original selection and accounting identity remain in the artifact.
    """

    def __init__(self, selection: Selection, *, accounting_id: str):
        if not accounting_id or selection.cost_micros is None:
            raise ValueError("recorded selection requires resolved linked accounting")
        self.selection, self.accounting_id = selection, accounting_id
        self.model = selection.model

    def maximum_charge_micros(self, objective, observation, decisions):
        return 0

    def select(self, selection_id, objective, observation, decisions, *, cancel, deadline):
        if cancel.is_set():
            raise ProviderFailure("cancelled", submitted=False, uncharged=True)
        self.selection.validate_answers(decisions)
        return self.selection.model_copy(
            update={
                "cost_micros": 0,
                "raw_response": {
                    "recorded_selection": self.selection.model_dump(mode="json"),
                    "accounting_id": self.accounting_id,
                    "contribution": "selector",
                },
            }
        )

    def lookup(self, attempt_id):
        return None
