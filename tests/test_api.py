"""HTTP-level checks for POST /api/revise and its interaction with /api/review."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


REVISE_BODY = {
    "nodes": [
        {
            "id": "users",
            "type": "source",
            "columns": [
                {"name": "uid", "sensitive": False},
                {"name": "email", "sensitive": True},
                {"name": "country", "sensitive": False},
            ],
        },
        {
            "id": "only_cn",
            "type": "filter",
            "input": "users",
            "references": ["country"],
        },
        {
            "id": "export_users",
            "type": "projection",
            "input": "only_cn",
            "columns": [
                {"name": "user_id", "copy": "uid"},
                {"name": "mail", "copy": "email"},
                {"name": "bucket", "constant": "ALL"},
            ],
        },
    ],
    "export": "export_users",
    "allowed_columns": ["mail"],
}


def test_revise_then_review_round_trip():
    resp = client.post("/api/revise", json=REVISE_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["revisable"] is True
    assert body["export"] == "revise_null_0"
    assert body["replaced_columns"] == ["mail"]
    assert body["review"]["blocked"] is False

    review_resp = client.post(
        "/api/review",
        json={"nodes": body["nodes"], "export": body["export"]},
    )
    assert review_resp.status_code == 200
    reviewed = review_resp.json()
    assert reviewed["blocked"] is False
    assert reviewed == body["review"]


def test_revise_row_influence_is_not_revisable():
    resp = client.post(
        "/api/revise",
        json={
            "nodes": [
                {
                    "id": "s",
                    "type": "source",
                    "columns": [
                        {"name": "id_card", "sensitive": True},
                        {"name": "score"},
                    ],
                },
                {
                    "id": "f",
                    "type": "filter",
                    "input": "s",
                    "references": ["id_card"],
                },
            ],
            "export": "f",
            "allowed_columns": ["id_card", "score"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["revisable"] is False
    assert {r["code"] for r in body["reasons"]} == {"ROW_INFLUENCE"}
    assert "nodes" not in body


def test_revise_rejects_duplicate_allowed_columns():
    resp = client.post(
        "/api/revise", json={**REVISE_BODY, "allowed_columns": ["mail", "mail"]}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "COLUMN_CONFLICT"


def test_revise_rejects_unknown_allowed_column():
    resp = client.post(
        "/api/revise", json={**REVISE_BODY, "allowed_columns": ["nope"]}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REFERENCE"


def test_revise_rejects_bad_graph_like_review():
    resp = client.post(
        "/api/revise",
        json={
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]}
            ],
            "export": "missing",
            "allowed_columns": [],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REFERENCE"


def test_revise_unknown_field_is_422():
    resp = client.post(
        "/api/revise", json={**REVISE_BODY, "bogus": 1}
    )
    assert resp.status_code == 422


def test_review_endpoint_unchanged():
    resp = client.post(
        "/api/review",
        json={"nodes": REVISE_BODY["nodes"], "export": REVISE_BODY["export"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is True
    assert {c["name"]: c["blocked"] for c in body["columns"]} == {
        "user_id": False,
        "mail": True,
        "bucket": False,
    }
