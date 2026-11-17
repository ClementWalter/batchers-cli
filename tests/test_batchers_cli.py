#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "requests==2.32.5",
#     "eth-abi==5.2.0",
#     "eth-utils==5.3.1",
#     "pycryptodome==3.23.0",
#     "rich==15.0.0",
#     "pytest==8.4.2",
# ]
#
# [tool.uv]
# exclude-newer = "2026-11-17T00:00:00Z"
# ///
"""Offline tests for batchers_cli: a stub JSON-RPC node answers the batcher getters."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batchers_cli as bc  # noqa: E402

NOW = 1_800_000_000
DEPOSIT = "0x0000000000000000000000000000000000001234"
REDEEM_0 = "0x0000000000000000000000000000000000001235"
REDEEM_1 = "0x0000000000000000000000000000000000001236"
OWNER = "0x0000000000000000000000000000000000000abc"


def selector(signature: str) -> str:
    return function_signature_to_4byte_selector(signature).hex()


# Per batcher: own fields, and per batch id (state, createdAt, dispatchedAt, minAge, callbackDeadline).
CHAIN = {
    DEPOSIT: {
        "paused": False,
        "currentBatchId": 5,
        "minBatchAge": 86280,
        "batches": {5: (0, NOW - 2000, 0, 1800, 43200), 4: (2, NOW - 5000, NOW - 1000, 1800, 43200)},
    },
    REDEEM_0: {
        "paused": False,
        "currentBatchId": 3,
        "minBatchAge": 1800,
        "batches": {3: (0, NOW - 4000, 0, 1800, 43200), 2: (1, NOW - 9000, NOW - 4000, 1800, 43200)},
    },
    REDEEM_1: {
        "paused": True,
        "currentBatchId": 1,
        "minBatchAge": 1,
        "batches": {1: (0, NOW - 100, 0, 1, 43200)},
    },
}


def answer(to: str, data: str) -> str:
    """The ABI-encoded return of `data` on the stub batcher `to`."""
    batcher = CHAIN[to]
    sel, args = data[2:10], bytes.fromhex(data[10:])
    own = {
        selector("paused()"): (["bool"], batcher["paused"]),
        selector("owner()"): (["address"], OWNER),
        selector("minBatchAge()"): (["uint256"], batcher["minBatchAge"]),
        selector("currentBatchId()"): (["uint256"], batcher["currentBatchId"]),
    }
    if sel in own:
        types, value = own[sel]
        return "0x" + encode(types, [value]).hex()
    batch_id = decode(["uint256"], args)[0]
    fields = dict(zip(["batchState", "batchCreatedAt", "batchDispatchedAt", "batchMinBatchAge", "batchCallbackDeadline"],
                      batcher["batches"][batch_id], strict=True))
    for name, value in fields.items():
        if sel == selector(f"{name}(uint256)"):
            return "0x" + encode(["uint256"], [value]).hex()
    raise AssertionError(f"unexpected call {data}")


class StubResponse:
    def __init__(self, body, status: int = 200):
        self.body, self.status_code, self.headers, self.text = body, status, {}, json.dumps(body)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return self.body


class StubSession:
    """A JSON-RPC node over CHAIN; `chain_id` is what eth_chainId answers."""

    def __init__(self, chain_id: int = 1):
        self.headers: dict[str, str] = {}
        self.chain_id = chain_id

    def post(self, _url, json, timeout):  # noqa: A002 - mirrors requests' keyword
        out = []
        for call in json:
            if call["method"] == "eth_chainId":
                out.append({"id": call["id"], "result": hex(self.chain_id)})
            else:
                params = call["params"][0]
                out.append({"id": call["id"], "result": answer(params["to"], params["data"])})
        return StubResponse(out)


@pytest.fixture
def book(tmp_path: Path) -> Path:
    path = tmp_path / "mainnet.json"
    path.write_text(json.dumps({
        "chainId": 1,
        "vaults": {"curator-usdc": {"depositBatcher": DEPOSIT, "redeemBatchers": [REDEEM_0, REDEEM_1]}},
    }))
    return path


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> StubSession:
    session = StubSession()
    original = bc.Rpc.__init__
    monkeypatch.setattr(bc.Rpc, "__init__", lambda self, url, session_=None: original(self, url, session))
    monkeypatch.setattr(bc.time, "time", lambda: NOW)
    monkeypatch.setenv("BATCHERS_CLI_CONFIG", "/nonexistent")
    return session


def schedule(book: Path, *args: str) -> list[dict]:
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule", "--json", *args])
    return json.loads(result.output)


def by_side(records: list[dict]) -> dict[str, dict]:
    return {r["side"]: r for r in records}


@pytest.mark.parametrize(
    ("side", "waiting"),
    [("deposit", "due"), ("redeem-0", "previous-batch"), ("redeem-1", "paused")],
)
def test_schedule_names_what_each_batcher_waits_on(book, chain, side, waiting):
    assert by_side(schedule(book))[side]["waiting"] == waiting


def test_next_dispatch_is_creation_plus_pinned_age(book, chain):
    assert by_side(schedule(book))["deposit"]["nextDispatchAt"] == bc.iso(NOW - 2000 + 1800)


def test_previous_batch_state_is_named(book, chain):
    assert by_side(schedule(book))["redeem-0"]["previousState"] == "Dispatched"


def test_genesis_batch_has_no_previous(book, chain):
    assert by_side(schedule(book))["redeem-1"]["previousBatchId"] is None


def test_side_filter_keeps_redeem_batchers(book, chain):
    assert [r["side"] for r in schedule(book, "--side", "redeem")] == ["redeem-0", "redeem-1"]


def test_waiting_filter_is_client_side(book, chain):
    assert [r["side"] for r in schedule(book, "--waiting", "paused")] == ["redeem-1"]


def test_limit_keeps_the_soonest(book, chain):
    assert [r["side"] for r in schedule(book, "--limit", "1")] == ["redeem-0"]


def test_unknown_vault_names_the_fix(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule", "--vault", "nope"])
    assert "batchers vaults" in result.output


def test_wrong_chain_rpc_is_refused(book, chain):
    chain.chain_id = 11155111
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule"])
    assert "fix --rpc-url" in result.output


def test_batcher_by_slug_shows_history(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "batcher", "curator-usdc/redeem-0", "--history", "2", "--json"])
    assert [b["state"] for b in json.loads(result.output)[0]["batches"]] == ["Pending", "Dispatched"]


def test_unknown_side_lists_the_vault_sides(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "batcher", "curator-usdc/redeem-9"])
    assert "deposit, redeem-0, redeem-1" in result.output


def test_vaults_reads_no_chain(book, monkeypatch):
    monkeypatch.setenv("BATCHERS_CLI_CONFIG", "/nonexistent")
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "vaults", "--json"])
    assert [r["side"] for r in json.loads(result.output)] == ["deposit", "redeem-0", "redeem-1"]


def test_missing_address_book_names_the_config(monkeypatch):
    monkeypatch.setenv("BATCHERS_CLI_CONFIG", "/nonexistent")
    monkeypatch.delenv("BATCHERS_ADDRESS_BOOK", raising=False)
    result = CliRunner().invoke(bc.cli, ["vaults"])
    assert "--address-book" in result.output


def test_schedule_pretty_titles_each_vault(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule", "--pretty"])
    assert "curator-usdc" in result.output


def test_batcher_pretty_lists_its_batches(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "batcher", "curator-usdc/redeem-0", "--history", "2", "--pretty"])
    assert "Dispatched" in result.output


@pytest.mark.parametrize(("side", "order"), [("deposit", (0, 0)), ("redeem-10", (1, 10))])
def test_side_order_puts_deposit_first(side, order):
    assert bc.side_order(side) == order


@pytest.mark.parametrize(("seconds", "text"), [(45, "45s"), (1800, "30m"), (86280, "23h58m"), (518400, "6d00h")])
def test_duration(seconds, text):
    assert bc.duration(seconds) == text


@pytest.mark.parametrize(("seconds", "text"), [(240, "in 4m"), (-13, "13s ago")])
def test_relative(seconds, text):
    assert bc.relative(seconds) == text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
