import pytest

from app.engine import ValidationError, revise, review
from app.models import ReviseRequest, ReviewRequest
from pydantic import ValidationError as PydanticValidationError


def revise_call(payload):
    return revise(ReviseRequest.model_validate(payload))


def review_payload(payload):
    return review(ReviewRequest.model_validate(payload))


USERS_GRAPH = {
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
}


# ----------------------------- safe graph ----------------------------------


def test_safe_graph_returned_unchanged_without_new_node():
    payload = {
        "nodes": [
            {
                "id": "s",
                "type": "source",
                "columns": [{"name": "a", "sensitive": False}],
            },
            {
                "id": "p",
                "type": "projection",
                "input": "s",
                "columns": [{"name": "a", "copy": "a"}],
            },
        ],
        "export": "p",
        "allowed_columns": ["a"],
    }
    res = revise_call(payload)
    assert res["revisable"] is True
    assert res["export"] == "p"
    assert res["replaced_columns"] == []
    # No nodes added; originals are returned verbatim.
    assert len(res["nodes"]) == 2
    assert res["nodes"] == payload["nodes"]
    assert res["review"]["blocked"] is False


# ----------------------------- successful revision -------------------------


def test_content_blocked_column_is_nulled_by_appended_projection():
    payload = {**USERS_GRAPH, "allowed_columns": ["mail"]}
    res = revise_call(payload)
    assert res["revisable"] is True
    assert res["replaced_columns"] == ["mail"]

    original_ids = {n["id"] for n in USERS_GRAPH["nodes"]}
    assert len(res["nodes"]) == 4
    # Original nodes untouched and in original order.
    assert res["nodes"][:3] == USERS_GRAPH["nodes"]

    new_node = res["nodes"][-1]
    assert new_node["id"] not in original_ids
    assert new_node["id"] == res["export"]
    assert new_node["type"] == "projection"
    assert new_node["input"] == "export_users"
    assert new_node["columns"] == [
        {"name": "user_id", "copy": "user_id"},
        {"name": "mail", "constant": None},
        {"name": "bucket", "copy": "bucket"},
    ]

    # The returned graph is accepted by /api/review and judged safe.
    re_review = review_payload({"nodes": res["nodes"], "export": res["export"]})
    assert re_review == res["review"]
    assert re_review["blocked"] is False
    sanitized = {c["name"]: c for c in re_review["columns"]}
    assert sanitized["mail"]["content_reasons"] == []
    assert sanitized["mail"]["row_reasons"] == []
    # Names, order and the untouched columns survive.
    assert [c["name"] for c in re_review["columns"]] == [
        "user_id",
        "mail",
        "bucket",
    ]
    assert sanitized["user_id"]["blocked"] is False
    assert sanitized["bucket"]["blocked"] is False


def test_only_columns_with_sensitive_provenance_are_actually_replaced():
    payload = {**USERS_GRAPH, "allowed_columns": ["mail", "user_id"]}
    res = revise_call(payload)
    assert res["revisable"] is True
    # user_id is clean, so authorizing it changes nothing and it is copied.
    assert res["replaced_columns"] == ["mail"]
    new_node = res["nodes"][-1]
    assert new_node["columns"][0] == {"name": "user_id", "copy": "user_id"}
    assert new_node["columns"][1] == {"name": "mail", "constant": None}


def test_join_export_with_sensitive_columns_can_be_sanitized():
    payload = {
        "nodes": [
            {
                "id": "l",
                "type": "source",
                "columns": [
                    {"name": "k"},
                    {"name": "secret", "sensitive": True},
                ],
            },
            {
                "id": "r",
                "type": "source",
                "columns": [{"name": "k"}, {"name": "label"}],
            },
            {
                "id": "j",
                "type": "join",
                "left_input": "l",
                "right_input": "r",
                "left_key": "k",
                "right_key": "k",
                "outputs": [
                    {"alias": "a_secret", "side": "left", "column": "secret"},
                    {"alias": "a_label", "side": "right", "column": "label"},
                ],
            },
        ],
        "export": "j",
        "allowed_columns": ["a_secret"],
    }
    res = revise_call(payload)
    assert res["revisable"] is True
    assert res["replaced_columns"] == ["a_secret"]
    new_node = res["nodes"][-1]
    assert new_node["input"] == "j"
    assert new_node["columns"] == [
        {"name": "a_secret", "constant": None},
        {"name": "a_label", "copy": "a_label"},
    ]
    assert res["review"]["blocked"] is False


# ----------------------------- not revisable -------------------------------


def test_row_influence_cannot_be_revised_even_with_all_columns_allowed():
    payload = {
        "nodes": [
            {
                "id": "s",
                "type": "source",
                "columns": [
                    {"name": "id_card", "sensitive": True},
                    {"name": "score", "sensitive": False},
                ],
            },
            {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
            {
                "id": "e",
                "type": "projection",
                "input": "f",
                "columns": [
                    {"name": "score", "copy": "score"},
                    {"name": "lit", "constant": 1},
                ],
            },
        ],
        "export": "e",
        "allowed_columns": ["score", "lit"],
    }
    res = revise_call(payload)
    assert res["revisable"] is False
    codes = {r["code"] for r in res["reasons"]}
    assert "ROW_INFLUENCE" in codes
    # No partially repaired graph is returned.
    assert "nodes" not in res
    assert "export" not in res


def test_sensitive_join_key_row_taint_is_not_revisable():
    payload = {
        "nodes": [
            {
                "id": "orders",
                "type": "source",
                "columns": [
                    {"name": "uid_enc", "sensitive": True},
                    {"name": "amount"},
                ],
            },
            {
                "id": "users",
                "type": "source",
                "columns": [{"name": "uid"}, {"name": "name"}],
            },
            {
                "id": "j",
                "type": "join",
                "left_input": "orders",
                "right_input": "users",
                "left_key": "uid_enc",
                "right_key": "uid",
                "outputs": [
                    {"alias": "amt", "side": "left", "column": "amount"},
                    {"alias": "uname", "side": "right", "column": "name"},
                ],
            },
        ],
        "export": "j",
        "allowed_columns": ["amt", "uname"],
    }
    res = revise_call(payload)
    assert res["revisable"] is False
    assert {r["code"] for r in res["reasons"]} == {"ROW_INFLUENCE"}


def test_blocked_column_outside_allow_list_is_not_revisable():
    payload = {**USERS_GRAPH, "allowed_columns": []}
    res = revise_call(payload)
    assert res["revisable"] is False
    reason = next(r for r in res["reasons"] if r["code"] == "BLOCKED_COLUMN_NOT_ALLOWED")
    assert reason["columns"] == ["mail"]
    assert "nodes" not in res


def test_unallowed_blocked_column_reported_alongside_allowed_ones():
    payload = {
        "nodes": [
            {
                "id": "s",
                "type": "source",
                "columns": [
                    {"name": "a", "sensitive": True},
                    {"name": "b", "sensitive": True},
                ],
            }
        ],
        "export": "s",
        "allowed_columns": ["a"],
    }
    res = revise_call(payload)
    assert res["revisable"] is False
    reason = next(
        r for r in res["reasons"] if r["code"] == "BLOCKED_COLUMN_NOT_ALLOWED"
    )
    assert reason["columns"] == ["b"]


def test_appending_beyond_60_nodes_is_not_revisable():
    nodes = [
        {"id": "s0", "type": "source", "columns": [{"name": "a", "sensitive": True}]}
    ]
    for i in range(1, 60):
        nodes.append(
            {
                "id": f"n{i}",
                "type": "projection",
                "input": f"n{i - 1}" if i > 1 else "s0",
                "columns": [{"name": "a", "copy": "a"}],
            }
        )
    payload = {"nodes": nodes, "export": "n59", "allowed_columns": ["a"]}
    assert len(nodes) == 60
    res = revise_call(payload)
    assert res["revisable"] is False
    assert {r["code"] for r in res["reasons"]} == {"NODE_LIMIT"}
    assert "nodes" not in res


def test_59_nodes_can_still_be_revised():
    nodes = [
        {"id": "s0", "type": "source", "columns": [{"name": "a", "sensitive": True}]}
    ]
    for i in range(1, 59):
        nodes.append(
            {
                "id": f"n{i}",
                "type": "projection",
                "input": f"n{i - 1}" if i > 1 else "s0",
                "columns": [{"name": "a", "copy": "a"}],
            }
        )
    payload = {"nodes": nodes, "export": "n58", "allowed_columns": ["a"]}
    assert len(nodes) == 59
    res = revise_call(payload)
    assert res["revisable"] is True
    assert len(res["nodes"]) == 60
    assert res["review"]["blocked"] is False


# ----------------------------- request validation --------------------------


def expect_revise_error(payload, code):
    with pytest.raises(ValidationError) as exc:
        revise_call(payload)
    assert exc.value.code == code


def test_duplicate_allowed_columns_rejected():
    expect_revise_error(
        {**USERS_GRAPH, "allowed_columns": ["mail", "mail"]},
        "COLUMN_CONFLICT",
    )


def test_unknown_allowed_column_rejected():
    expect_revise_error(
        {**USERS_GRAPH, "allowed_columns": ["nope"]},
        "INVALID_REFERENCE",
    )


def test_invalid_graph_still_rejected_by_revise():
    expect_revise_error(
        {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]}
            ],
            "export": "ghost",
            "allowed_columns": ["a"],
        },
        "INVALID_REFERENCE",
    )
    expect_revise_error(
        {
            "nodes": [
                {"id": "a", "type": "projection", "input": "b",
                 "columns": [{"name": "x", "constant": 1}]},
                {"id": "b", "type": "projection", "input": "a",
                 "columns": [{"name": "x", "constant": 1}]},
            ],
            "export": "a",
            "allowed_columns": ["x"],
        },
        "CYCLE",
    )


def test_missing_allowed_columns_is_a_schema_error():
    with pytest.raises(PydanticValidationError):
        ReviseRequest.model_validate(
            {"nodes": USERS_GRAPH["nodes"], "export": "export_users"}
        )


def test_more_than_60_nodes_rejected_by_revise():
    nodes = [
        {"id": "s0", "type": "source", "columns": [{"name": "a"}]}
    ]
    for i in range(1, 61):
        nodes.append(
            {
                "id": f"n{i}",
                "type": "projection",
                "input": f"n{i - 1}" if i > 1 else "s0",
                "columns": [{"name": "a", "copy": "a"}],
            }
        )
    with pytest.raises(PydanticValidationError):
        ReviseRequest.model_validate(
            {"nodes": nodes, "export": "n60", "allowed_columns": ["a"]}
        )


# ----------------------------- id generation -------------------------------


def test_projection_id_avoids_existing_nodes():
    payload = {**USERS_GRAPH, "allowed_columns": ["mail"]}
    # Pre-create a node using the default sanitizer id.
    clash = {
        "id": "revise_null_0",
        "type": "projection",
        "input": "export_users",
        "columns": [{"name": "x", "constant": 1}],
    }
    # The clash node itself needs a valid place; insert it as the export chain
    # is left intact by exporting the original node.
    payload = {
        "nodes": USERS_GRAPH["nodes"] + [clash],
        "export": "export_users",
        "allowed_columns": ["mail"],
    }
    res = revise_call(payload)
    assert res["revisable"] is True
    assert res["export"] == "revise_null_1"
    assert res["nodes"][-1]["id"] == "revise_null_1"
    assert review_payload({"nodes": res["nodes"], "export": res["export"]})[
        "blocked"
    ] is False
