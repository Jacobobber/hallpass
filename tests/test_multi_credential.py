"""Some services need more than one credential (Datadog's API key + app key).
The vault holds one credential per (subject, service), so a multi-credential
service stores a JSON bundle and the `multi` auth style places each field in
its header/query. What matters: both credentials reach the request, a malformed
bundle is a clean ConnectorError (not a crash or a partial call), and the
bundle is never echoed in an error."""

import json

import pytest

from hallpass import ConnectorError, catalog, dev_app
from hallpass.rest import _apply_multi


def test_apply_multi_places_each_field():
    spec = (
        ("header", "DD-API-KEY", "api_key"),
        ("header", "DD-APPLICATION-KEY", "app_key"),
    )
    headers, params = _apply_multi(
        json.dumps({"api_key": "AAA", "app_key": "BBB"}), spec
    )
    assert headers == {"DD-API-KEY": "AAA", "DD-APPLICATION-KEY": "BBB"}
    assert params == {}


def test_apply_multi_supports_query_placement():
    spec = (("query", "api_key", "k"),)
    headers, params = _apply_multi(json.dumps({"k": "V"}), spec)
    assert headers == {} and params == {"api_key": "V"}


def test_apply_multi_rejects_non_json():
    with pytest.raises(ConnectorError):
        _apply_multi("not json", (("header", "X", "f"),))


def test_apply_multi_rejects_missing_field():
    with pytest.raises(ConnectorError) as exc:
        _apply_multi(json.dumps({"api_key": "A"}), (("header", "H", "app_key"),))
    assert "app_key" in str(exc.value)
    assert "A" not in str(exc.value)  # the present credential is not echoed


class RecordingHttp:
    def __init__(self):
        self.last = None

    def request(self, method, url, *, headers, params, json, data=None):
        self.last = {"headers": headers}
        return [{"id": 1}]


def test_datadog_sends_both_keys_end_to_end():
    http = RecordingHttp()
    app, token = dev_app(connectors=[catalog.load("datadog", http=http)])
    app._vault.store(
        "alice", "datadog", json.dumps({"api_key": "AAA", "app_key": "BBB"})
    )
    out = app.call_tool(token("alice", ["datadog:read"]), "datadog_list_monitors", {})
    assert out == [{"id": 1}]
    assert http.last["headers"]["DD-API-KEY"] == "AAA"
    assert http.last["headers"]["DD-APPLICATION-KEY"] == "BBB"
    app.close()


def test_datadog_bad_bundle_is_a_clean_error():
    http = RecordingHttp()
    app, token = dev_app(connectors=[catalog.load("datadog", http=http)])
    app._vault.store("alice", "datadog", "not-a-json-bundle")
    with pytest.raises(ConnectorError):
        app.call_tool(token("alice", ["datadog:read"]), "datadog_list_monitors", {})
    app.close()
