from test_motor import setup as make_setup

from environment_harness.motor_contracts import MotorSelection


class OneCandidateSelector:
    model = "test-pinned"
    endpoint = "https://selector.test"

    def __init__(self):
        self.calls = 0

    def maximum_cost(self, *_):
        return 1

    def select(self, _state, candidates, **_kwargs):
        self.calls += 1
        return MotorSelection(candidate_id=candidates[0].id, model=self.model, cost_micros=4)


def test_configured_jev_selects_single_legal_candidate_before_effect(tmp_path):
    selector = OneCandidateSelector()
    driver, _, prepare, dispatch, *_ = make_setup(tmp_path, selector=selector)
    prepare()
    receipt = dispatch()
    assert receipt["status"] == "completed"
    assert receipt["cost_micros"] == 4
    assert selector.calls == 1
    assert len(driver.calls) == 1
