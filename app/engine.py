"""Sensitive-provenance analysis over the node graph.

Two independent kinds of provenance are tracked for every node:

* ``content_origins`` (per column): sensitive source columns whose *values*
  reach the column. Copy propagates them; a constant replacement clears them.
* ``row_origins`` (per relation): sensitive source columns that can influence
  *which rows* the relation contains. Filters referencing a sensitive column,
  and joins whose join key carries sensitive content, taint the whole result;
  projections and constant replacements can never remove that taint, and a
  join merges the taint of both inputs.

A column of the export node is blocked when either set is non-empty.
"""

from __future__ import annotations

from collections import defaultdict, deque

from .models import (
    FilterNodeIn,
    JoinNodeIn,
    ProjectionNodeIn,
    ReviewRequest,
    ReviseRequest,
    SourceNodeIn,
)


class ValidationError(Exception):
    """The whole request must be rejected (HTTP 400)."""

    def __init__(self, code: str, message: str, node_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_id = node_id

    def detail(self) -> dict[str, object]:
        d: dict[str, object] = {"code": self.code, "message": self.message}
        if self.node_id is not None:
            d["node_id"] = self.node_id
        return d


# A sensitive origin is a (source node id, source column name) pair.
Origin = tuple[str, str]


class Column:
    __slots__ = ("name", "content_origins")

    def __init__(self, name: str, content_origins: frozenset[Origin]):
        self.name = name
        self.content_origins = content_origins


class Relation:
    __slots__ = ("columns", "row_origins")

    def __init__(self, columns: list[Column], row_origins: frozenset[Origin]):
        self.columns = columns
        self.row_origins = row_origins

    def column_map(self) -> dict[str, Column]:
        return {c.name: c for c in self.columns}


def _unique(names: list[str], what: str, node_id: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValidationError(
                "COLUMN_CONFLICT",
                f"{what} '{name}' is declared more than once in node '{node_id}'",
                node_id,
            )
        seen.add(name)


def _inputs(node: object) -> list[str]:
    if isinstance(node, (ProjectionNodeIn, FilterNodeIn)):
        return [node.input]
    if isinstance(node, JoinNodeIn):
        return [node.left_input, node.right_input]
    return []  # source nodes have no inputs


def topological_order(by_id: dict[str, object]) -> list[str]:
    """Return every node in dependency order.

    Edges go from an input node to its consumer. A request is rejected when an
    input id is unknown or the graph contains a cycle (self-loop included).
    The *whole* request is validated even for nodes the export does not use.
    """
    indegree: dict[str, int] = {nid: 0 for nid in by_id}
    edges: dict[str, list[str]] = defaultdict(list)
    for nid, node in by_id.items():
        for ref in _inputs(node):
            if ref not in by_id:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"node '{nid}' references unknown input node '{ref}'",
                    nid,
                )
            edges[ref].append(nid)
            indegree[nid] += 1

    # Deterministic traversal: ids sorted alphabetically.
    ready = deque(sorted(nid for nid, d in indegree.items() if d == 0))
    order: list[str] = []
    while ready:
        nid = ready.popleft()
        order.append(nid)
        for consumer in sorted(edges[nid]):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)

    if len(order) != len(by_id):
        stuck = sorted(nid for nid, d in indegree.items() if d > 0)
        raise ValidationError(
            "CYCLE",
            "dependency cycle detected involving nodes: " + ", ".join(stuck),
        )
    return order


def _eval_source(node: SourceNodeIn) -> Relation:
    columns = [
        Column(
            col.name,
            frozenset({(node.id, col.name)}) if col.sensitive else frozenset(),
        )
        for col in node.columns
    ]
    return Relation(columns, frozenset())


def _eval_projection(node: ProjectionNodeIn, upstream: Relation) -> Relation:
    available = upstream.column_map()
    columns: list[Column] = []
    for spec in node.columns:
        if spec.is_constant:
            # A constant replacement clears the column's content provenance.
            columns.append(Column(spec.name, frozenset()))
        else:
            if spec.copy_name not in available:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"projection node '{node.id}' copies unknown column "
                    f"'{spec.copy_name}' from input '{node.input}'",
                    node.id,
                )
            columns.append(Column(spec.name, available[spec.copy_name].content_origins))
    # Projection cannot remove row-level influence of upstream operations.
    return Relation(columns, upstream.row_origins)


def _eval_filter(node: FilterNodeIn, upstream: Relation) -> Relation:
    available = upstream.column_map()
    row_origins = set(upstream.row_origins)
    for ref in node.references:
        if ref not in available:
            raise ValidationError(
                "INVALID_REFERENCE",
                f"filter node '{node.id}' references unknown column "
                f"'{ref}' from input '{node.input}'",
                node.id,
            )
        # A sensitive predicate column affects every surviving row.
        row_origins |= available[ref].content_origins
    # The filter keeps all input columns, contents untouched.
    return Relation(
        [Column(c.name, c.content_origins) for c in upstream.columns],
        frozenset(row_origins),
    )


def _eval_join(
    node: JoinNodeIn, left: Relation, right: Relation
) -> Relation:
    left_cols = left.column_map()
    right_cols = right.column_map()

    for side_name, key, cols, input_id in (
        ("left", node.left_key, left_cols, node.left_input),
        ("right", node.right_key, right_cols, node.right_input),
    ):
        if key not in cols:
            raise ValidationError(
                "INVALID_REFERENCE",
                f"join node '{node.id}' uses unknown {side_name} key '{key}' "
                f"from input '{input_id}'",
                node.id,
            )

    # Row influence: both inputs' influence, plus sensitive key content on
    # either side, affects every row of the joined result.
    row_origins = (
        left.row_origins
        | right.row_origins
        | left_cols[node.left_key].content_origins
        | right_cols[node.right_key].content_origins
    )

    columns: list[Column] = []
    for out in node.outputs:
        if out.side == "left":
            if out.column not in left_cols:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"join node '{node.id}' outputs unknown left column "
                    f"'{out.column}'",
                    node.id,
                )
            columns.append(Column(out.alias, left_cols[out.column].content_origins))
        else:
            if out.column not in right_cols:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"join node '{node.id}' outputs unknown right column "
                    f"'{out.column}'",
                    node.id,
                )
            columns.append(Column(out.alias, right_cols[out.column].content_origins))
    return Relation(columns, row_origins)


def _origin_view(origins: frozenset[Origin]) -> list[dict[str, str]]:
    return [
        {"node_id": node_id, "column": column}
        for node_id, column in sorted(origins)
    ]


def _column_view(column: Column, row_origins: frozenset[Origin]) -> dict[str, object]:
    blocked = bool(column.content_origins) or bool(row_origins)
    return {
        "name": column.name,
        "blocked": blocked,
        "content_reasons": _origin_view(column.content_origins),
        "row_reasons": _origin_view(row_origins),
    }


def _analyze(
    nodes: list[object], export_id: str
) -> tuple[dict[str, object], list[str], str, Relation]:
    """Validate the whole graph and evaluate every node.

    Runs exactly the structural validation and provenance rules of
    ``POST /api/review`` and returns the id-indexed nodes, the topological
    order, and the export node's relation.
    """
    by_id: dict[str, object] = {}
    for node in nodes:
        if node.id in by_id:
            raise ValidationError(
                "DUPLICATE_NODE",
                f"node id '{node.id}' is used more than once",
                node.id,
            )
        by_id[node.id] = node

        if isinstance(node, SourceNodeIn):
            _unique([c.name for c in node.columns], "column", node.id)
        elif isinstance(node, ProjectionNodeIn):
            _unique([c.name for c in node.columns], "output column", node.id)
        elif isinstance(node, FilterNodeIn):
            _unique(node.references, "reference", node.id)
        elif isinstance(node, JoinNodeIn):
            _unique([o.alias for o in node.outputs], "output alias", node.id)

    if export_id not in by_id:
        raise ValidationError(
            "INVALID_REFERENCE",
            f"export node '{export_id}' does not exist",
            export_id,
        )

    # Validate and evaluate the whole graph: invalid references in branches
    # the export does not use still reject the whole request. At most 60 nodes.
    order = topological_order(by_id)

    relations: dict[str, Relation] = {}
    for nid in order:
        node = by_id[nid]
        if isinstance(node, SourceNodeIn):
            relations[nid] = _eval_source(node)
        elif isinstance(node, ProjectionNodeIn):
            relations[nid] = _eval_projection(node, relations[node.input])
        elif isinstance(node, FilterNodeIn):
            relations[nid] = _eval_filter(node, relations[node.input])
        else:
            relations[nid] = _eval_join(
                node, relations[node.left_input], relations[node.right_input]
            )

    return by_id, order, export_id, relations[export_id]


def review(request: ReviewRequest) -> dict[str, object]:
    _by_id, _order, export_id, export_relation = _analyze(
        list(request.nodes), request.export
    )

    # --- review every column of the export node ----------------------------
    columns_out = [
        _column_view(column, export_relation.row_origins)
        for column in export_relation.columns
    ]
    any_blocked = any(c["blocked"] for c in columns_out)

    return {
        "export": export_id,
        "blocked": any_blocked,
        "columns": columns_out,
    }


# Prefix of the single projection node a revision is allowed to append.
REVISE_PROJECTION_PREFIX = "revise_null_"


def _revised_projection_id(by_id: dict[str, object]) -> str:
    """Return a projection id that does not collide with any original node."""
    nid = REVISE_PROJECTION_PREFIX + "0"
    counter = 1
    while nid in by_id:
        nid = f"{REVISE_PROJECTION_PREFIX}{counter}"
        counter += 1
    return nid


def revise(request: ReviseRequest) -> dict[str, object]:
    nodes = list(request.nodes)
    by_id, _order, export_id, export_relation = _analyze(nodes, request.export)

    # --- validate the caller-supplied replacement allow-list ---------------
    _unique(request.allowed_columns, "allowed replacement column", export_id)
    export_columns = {c.name for c in export_relation.columns}
    for name in request.allowed_columns:
        if name not in export_columns:
            raise ValidationError(
                "INVALID_REFERENCE",
                f"allowed replacement column '{name}' does not exist on export "
                f"node '{export_id}'",
                export_id,
            )
    allowed = set(request.allowed_columns)

    # --- classify export columns exactly as /api/review does ---------------
    column_views = [
        _column_view(column, export_relation.row_origins)
        for column in export_relation.columns
    ]
    any_blocked = any(c["blocked"] for c in column_views)

    # An already-safe graph is returned untouched: no new node, empty
    # replacement list, export unchanged.
    if not any_blocked:
        return {
            "revisable": True,
            "nodes": [
                node.model_dump(by_alias=True, exclude_unset=True)
                for node in nodes
            ],
            "export": export_id,
            "replaced_columns": [],
            "review": {"export": export_id, "blocked": False, "columns": column_views},
        }

    # --- decide whether a single appended projection can fix everything ----
    reasons: list[dict[str, object]] = []

    # A projection only rewrites column values; it can never remove row-level
    # influence, which taints every surviving row.
    if export_relation.row_origins:
        reasons.append(
            {
                "code": "ROW_INFLUENCE",
                "message": (
                    "the export relation is row-tainted by sensitive columns; a "
                    "column projection cannot remove row-level influence"
                ),
                "row_reasons": _origin_view(export_relation.row_origins),
            }
        )

    # A blocked column is fixable only when its values carry sensitive content
    # and the caller authorized replacing that exact column.
    not_allowed = [
        c["name"]
        for c in column_views
        if c["content_reasons"] and c["name"] not in allowed
    ]
    if not_allowed:
        reasons.append(
            {
                "code": "BLOCKED_COLUMN_NOT_ALLOWED",
                "message": (
                    "blocked export column(s) are not in the allowed replacement "
                    "list: " + ", ".join(not_allowed)
                ),
                "columns": not_allowed,
            }
        )

    replaced = [
        c.name
        for c in export_relation.columns
        if c.content_origins and c.name in allowed
    ]

    # The only permitted change is appending one projection; the resulting
    # graph must still satisfy the 60-node cap.
    if replaced and len(nodes) + 1 > 60:
        reasons.append(
            {
                "code": "NODE_LIMIT",
                "message": (
                    f"appending the sanitizing projection would raise the graph "
                    f"from {len(nodes)} to {len(nodes) + 1} nodes, exceeding the "
                    "60-node limit"
                ),
            }
        )

    # Refuse wholesale: never return a partially repaired graph.
    if reasons:
        return {"revisable": False, "reasons": reasons}

    # --- build the one appended projection ---------------------------------
    projection_id = _revised_projection_id(by_id)
    projection_columns: list[dict[str, object]] = []
    for column in export_relation.columns:
        if column.content_origins and column.name in allowed:
            projection_columns.append({"name": column.name, "constant": None})
        else:
            projection_columns.append(
                {"name": column.name, "copy": column.name}
            )
    sanitizer = {
        "id": projection_id,
        "type": "projection",
        "input": export_id,
        "columns": projection_columns,
    }

    revised_nodes = [
        node.model_dump(by_alias=True, exclude_unset=True) for node in nodes
    ] + [sanitizer]
    revised_payload = {"nodes": revised_nodes, "export": projection_id}

    # --- re-review under the exact existing rules --------------------------
    re_review = review(ReviewRequest.model_validate(revised_payload))
    if re_review["blocked"]:  # defensive: every reason above should prevent this
        raise ValidationError(
            "REVISION_FAILED",
            "the sanitized graph is still blocked under the existing review rules",
            projection_id,
        )

    return {
        "revisable": True,
        "nodes": revised_nodes,
        "export": projection_id,
        "replaced_columns": replaced,
        "review": re_review,
    }
