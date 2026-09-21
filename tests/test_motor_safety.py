import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_motor import Alternatives
from test_motor import request as make_request
from test_motor import setup as make_setup

from environment_harness.errors import Conflict, Forbidden
from environment_harness.motor import MotorOutcomeUnknown

pytestmark = pytest.mark.skipif(os.name == "nt", reason="MotorExecutor uses POSIX application locking")


class UnknownSelector:
    model = "test-pinned"
    endpoint = "https://selector.test"

    def maximum_cost(self, *_):
        return 1

    def select(self, *_args, **_kwargs):
        raise MotorOutcomeUnknown("selector outcome is unknown")


def test_selector_unknown_preserves_operation_reservation(tmp_path):
    driver, _, prepare, dispatch, session, who, environment, _ = make_setup(
        tmp_path, selector=UnknownSelector(), adapter_class=Alternatives
    )
    prepare()
    with pytest.raises(MotorOutcomeUnknown):
        dispatch()
    with session.store.transaction() as db:
        operation = db.execute(
            "SELECT status,reservation FROM operations WHERE environment=? AND id=?",
            (environment, "fill"),
        ).fetchone()
        balance = session.store.environment(db, environment, who)
    assert operation["status"] == "unknown"
    assert operation["reservation"] == 100
    assert balance["reserved"] == 100
    assert not driver.calls


def test_non_read_skill_cannot_escape_write_authorization(tmp_path):
    _, _, prepare, dispatch, *_ = make_setup(tmp_path)
    request = make_request().model_copy(update={"skill": "click", "arguments": {}})
    prepare(request, oid="read-only", write=False)
    with pytest.raises(Forbidden, match="write"):
        dispatch("read-only")


def test_motor_input_ownership_rejects_second_dispatch(tmp_path):
    driver, _, prepare, dispatch, *_ = make_setup(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocking(operation, payload, *, operation_id, cancel, deadline):
        entered.set()
        release.wait(3)
        return {"operation_id": operation_id, "status": "cancelled"}

    driver.execute = blocking
    prepare(oid="first")
    prepare(oid="second", maximum_cost_micros=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(dispatch, "first")
        assert entered.wait(3)
        with pytest.raises(Conflict, match="input owner"):
            dispatch("second")
        release.set()
        assert future.result(timeout=3)["status"] == "cancelled"
