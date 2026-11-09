"""arr's lists of Codex server requests and fields, held against the pinned app-server (W4-00).

`codex app-server generate-json-schema` needs no model and no login. It runs with the bundled
binary in a test `HOME`, with the experimental fields, because arr starts the client with the
SDK's default `experimentalApi: true` (S2 §10: real requests carry `availableDecisions`).

A new Codex version turns `test_the_schema_is_the_one_of_the_pinned_version` red: compare the
lists in `arr.backends.codex_approvals` with the new schema, then raise `SCHEMA_VERSION`.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from codex_cli_bin import bundled_codex_path

from arr.backends import codex_approvals as ca

UNANSWERED = frozenset({"currentTime/read"})
"""Server requests of 0.163.0 that arr does not answer on purpose: they get the JSON-RPC error
for an unknown method (H1). `currentTime/read` asks the client for the time, experimental."""


def _codex(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(home)}
    if "SYSTEMROOT" in os.environ:  # pragma: no cover - Windows only
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return subprocess.run(
        [str(bundled_codex_path()), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


@pytest.fixture(scope="module")
def schema(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("codex_schema")
    home, out = root / "home", root / "out"
    home.mkdir()
    _codex("app-server", "generate-json-schema", "--experimental", "--out", str(out), home=home)
    version = _codex("--version", home=home).stdout.split()[-1]

    def load(name: str) -> dict[str, Any]:
        return json.loads((out / f"{name}.json").read_text())

    return {
        "version": version,
        "requests": load("ServerRequest"),
        "command": load("CommandExecutionRequestApprovalParams"),
        "file": load("FileChangeRequestApprovalParams"),
        "v2": load("codex_app_server_protocol.v2.schemas"),
    }


def _fields(definition: dict[str, Any]) -> set[str]:
    return set(definition["properties"])


def test_the_schema_is_the_one_of_the_pinned_version(schema: dict[str, Any]) -> None:
    assert schema["version"] == ca.SCHEMA_VERSION, (
        f"Codex {schema['version']} statt {ca.SCHEMA_VERSION}: Gleiche die Methoden und Felder "
        "in arr/backends/codex_approvals.py mit dem neuen Schema ab (dieser Test), dann "
        "SCHEMA_VERSION anheben."
    )


def test_every_server_request_is_answered_on_purpose(schema: dict[str, Any]) -> None:
    methods = {v["properties"]["method"]["enum"][0] for v in schema["requests"]["oneOf"]}
    assert methods == ca.KNOWN_METHODS | UNANSWERED
    assert not ca.KNOWN_METHODS & UNANSWERED
    for method in UNANSWERED:
        assert ca.refusal(method)["code"] == ca.METHOD_NOT_FOUND


def test_command_fields_are_the_ones_of_the_schema(schema: dict[str, Any]) -> None:
    command = schema["command"]
    assert _fields(command) == ca.COMMAND_PARAM_FIELDS
    network = command["definitions"]["NetworkApprovalContext"]
    assert _fields(network) == ca.NETWORK_CONTEXT_FIELDS


def test_file_fields_are_the_ones_of_the_schema(schema: dict[str, Any]) -> None:
    assert _fields(schema["file"]) == ca.FILE_PARAM_FIELDS
    definitions = schema["v2"]["definitions"]
    assert _fields(definitions["FileUpdateChange"]) == ca.CHANGE_FIELDS
    kinds = definitions["PatchChangeKind"]["oneOf"]
    assert {k["properties"]["type"]["enum"][0] for k in kinds} == ca.CHANGE_KINDS
    assert set().union(*(_fields(k) for k in kinds)) == ca.CHANGE_KIND_FIELDS


def test_no_refusal_is_an_empty_result() -> None:
    """H1: `{}` is no refusal in the protocol, for no method arr knows or does not know."""
    for method in (*ca.KNOWN_METHODS, *UNANSWERED, "item/somethingNew/requestApproval"):
        assert ca.refusal(method) != {}, method
