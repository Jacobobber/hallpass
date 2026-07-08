"""Some services take form-urlencoded bodies, not JSON (Stripe's write API is
the canonical case). A form endpoint must send its body as `data` (which the
client form-encodes) and NOT as `json`, or the request is silently malformed.
The read path must be unaffected -- it never sets `data`, so JSON-only clients
(and every existing test fake) keep working."""

from hallpass import catalog, dev_app


class RecordingHttp:
    """Records how the body was sent. Accepts data= (a JSON-only fake would
    not, which is exactly why the handler only passes data= for form endpoints)."""

    def __init__(self):
        self.last = None

    def request(self, method, url, *, headers, params, json, data=None):
        self.last = {"method": method, "url": url, "json": json, "data": data}
        return {"id": "cus_1"}


def _stripe_app():
    http = RecordingHttp()
    app, token = dev_app(connectors=[catalog.load("stripe", http=http)])
    app._vault.store("alice", "stripe", "sk_test_x")
    return app, token, http


def test_form_endpoint_sends_body_as_data_not_json():
    app, token, http = _stripe_app()
    out = app.call_tool(
        token("alice", ["stripe:write"]),
        "stripe_create_customer",
        {"email": "a@b.com", "name": "Ada"},
    )
    assert out == {"id": "cus_1"}
    assert http.last["method"] == "POST"
    assert http.last["json"] is None  # not JSON
    assert http.last["data"] == {"email": "a@b.com", "name": "Ada"}  # form body
    app.close()


def test_form_endpoint_only_sends_provided_fields():
    app, token, http = _stripe_app()
    app.call_tool(
        token("alice", ["stripe:write"]),
        "stripe_create_customer",
        {"email": "solo@b.com"},  # name/description omitted
    )
    assert http.last["data"] == {"email": "solo@b.com"}
    app.close()


def test_read_endpoint_does_not_use_the_form_path():
    app, token, http = _stripe_app()
    app.call_tool(
        token("alice", ["stripe:read"]), "stripe_list_customers", {"limit": "3"}
    )
    assert http.last["method"] == "GET"
    assert http.last["data"] is None  # read path never sets data
    assert http.last["json"] is None  # GET carries no body
    app.close()


def test_catalog_marks_stripe_write_as_form():
    svc = catalog.SERVICES["stripe"]
    write = next(e for e in svc.endpoints if e.name == "stripe_create_customer")
    read = next(e for e in svc.endpoints if e.name == "stripe_list_customers")
    assert write.form is True
    assert read.form is False
