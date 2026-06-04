"""§4e — helperzz ``create_playbook`` orchestrator (no-functions 400 path).

Patches the AI sub-steps so the deterministic "can't generate" branch is hit:
``minimize_functions`` returning no functions → ``(jsonify(...), 400)``. Runs
inside a Flask app context so ``jsonify`` works.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.workflow_playbook import _wf_pb_stubs as stubs

stubs.bootstrap_sut()

import playbook.helperzz as h  # noqa: E402

pytestmark = pytest.mark.unit


def test_create_playbook_no_functions_returns_400():
    app = stubs.make_app()
    data = {"user_id": "u1", "title": "T", "description": "d",
            "contacts": [], "communication_channels": []}
    with app.app_context(), \
         patch.object(h, "ensure_dir", MagicMock()), \
         patch.object(h, "needs_internal_data", AsyncMock(return_value=False)), \
         patch.object(h, "minimize_functions", AsyncMock(return_value=(None, None))):
        _resp, code = asyncio.run(
            h.create_playbook(data, {}, {}, {}, db=MagicMock(), credits=MagicMock()))
    assert code == 400
