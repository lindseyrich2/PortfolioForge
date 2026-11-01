"""Opt-in live test against the Anthropic API, with a tiny SYNTHETIC evidence pack.

Skipped unless PORTFOLIOFORGE_RUN_NETWORK_TESTS=1 is set and the Anthropic SDK is
installed. It never reads any real project: the evidence below is invented.
Running it sends that synthetic evidence to Anthropic and incurs a small cost.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.network

RUN = os.environ.get("PORTFOLIOFORGE_RUN_NETWORK_TESTS") == "1"


@pytest.mark.skipif(not RUN, reason="network tests are opt-in (set PORTFOLIOFORGE_RUN_NETWORK_TESTS=1)")
def test_live_anthropic_round_trip_with_synthetic_evidence():
    pytest.importorskip("anthropic")
    from portfolioforge.understanding import approve_manifest, build_manifest, get_provider, submit
    from portfolioforge.understanding.analysis import build_proposal
    from portfolioforge.understanding.models import EvidenceItem, EvidencePack, Technology

    text = "def fibonacci(n):\n    \"\"\"Return the n-th Fibonacci number recursively.\"\"\""
    item = EvidenceItem("e_live1", "f_live", "fib.py", "Python", "definition", 1, 3, None, text,
                        "0" * 64)
    pack = EvidencePack("p_live", "live", "g_live", "a_live", datetime.now(UTC), (item,),
                        (Technology("Python", ("e_live1",)),), ({"project_path": "fib.py"},), ())
    manifest, payload = build_manifest(pack, "anthropic", os.environ.get("PORTFOLIOFORGE_LIVE_MODEL",
                                                                         "claude-sonnet-5-5"), max_tokens=12000,
                                       effort="low")  # thinking counts toward max_tokens; leave room
    result = submit(manifest, payload, approve_manifest(manifest), get_provider("anthropic", manifest.model))
    assert result.sent and result.response, f"no usable answer: {result.status}"
    proposal = build_proposal(response_text=result.response, sent_body=json.loads(payload), inventory_key="live",
                              group_id="g_live", manifest_id=manifest.id, provider="anthropic", model=manifest.model)
    assert proposal.ownership == "unconfirmed"
