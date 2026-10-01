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


def review(request: ReviewRequest) -> dict[str, object]:
    # --- structural validation of the request -----------------------------
    by_id: dict[str, object] = {}
    for node in request.nodes:
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

    if request.export not in by_id:
        raise ValidationError(
            "INVALID_REFERENCE",
            f"export node '{request.export}' does not exist",
            request.export,
        )

    # Validate and evaluate the whole graph: invalid references in branches
    # the export does not use still reject the whole request. At most 60 nodes.
    order = topological_order(by_id)

    # --- provenance evaluation in dependency order -------------------------
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

    # --- review every column of the export node ----------------------------
    export_relation = relations[request.export]
    columns_out: list[dict[str, object]] = []
    any_blocked = False
    for column in export_relation.columns:
        content = column.content_origins
        rows = export_relation.row_origins
        blocked = bool(content) or bool(rows)
        any_blocked = any_blocked or blocked
        columns_out.append(
            {
                "name": column.name,
                "blocked": blocked,
                "content_reasons": _origin_view(content),
                "row_reasons": _origin_view(rows),
            }
        )

    return {
        "export": request.export,
        "blocked": any_blocked,
        "columns": columns_out,
    }
