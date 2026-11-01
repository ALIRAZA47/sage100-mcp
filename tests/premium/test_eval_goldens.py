"""The eval set's test bed goldens (evals/tasks.yaml) are what the server answers on SQL Server too, so the
eval set scores the same whichever backend serves it (SPEC.md §15.6)."""

from __future__ import annotations

import pytest

from sage100_mcp.runtime import Runtime
from tests.emulator.test_eval_goldens import Tools, load_tasks, server_answers

pytestmark = pytest.mark.premium


def test_goldens_hold_on_sql_server(runtime: Runtime) -> None:
    served = dict(server_answers(Tools(runtime)))
    stored = {task["id"]: str(task["golden"]["testbed"]) for task in load_tasks()}
    assert served == stored, {k: (stored[k], served.get(k)) for k in stored if served.get(k) != stored[k]}
