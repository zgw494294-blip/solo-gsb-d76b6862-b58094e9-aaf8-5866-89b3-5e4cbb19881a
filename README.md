# 本地数据导出风险审查 API

用 Python / FastAPI 实现的本地 HTTP 服务。输入一张**可乱序、至多 60 个节点**的数据加工图和一个
导出节点，服务沿依赖关系逐列追踪**敏感来源**，对导出节点的每一列给出审查结论：

* **内容影响（content）**：敏感源列的值流入了该列；
* **行影响（row）**：敏感源列影响了结果里“出现哪些行”，该影响会落到结果的**每一列、每一行**。

两类原因只要存在其一，该导出列即判 `blocked: true`，并分别列出源头节点 ID 与列名。

## 风险传播规则

| 节点 | 行为 | 内容影响 | 行影响 |
|---|---|---|---|
| `source` | 声明互异列名与 `sensitive` 标记 | 敏感列自身的来源 `(节点, 列)` | 初始为空 |
| `projection` | 只保留明确列出的输出列；每列 `copy` 上游列或以 `constant` 常量替换 | `copy` 继承上游列；`constant` **清除**该列内容来源 | 原样保留（投影/替换不能消除行影响） |
| `filter` | 声明条件 `references` 引用的列，保留全部输入列 | 输入列原样保留 | 并入所有被引用列的内容来源；并继承上游行影响 |
| `join` | 等值连接：左右输入、`left_key`/`right_key`、输出列与互异别名 | 每个输出列继承对应侧列的内容来源 | 合并两侧已有行影响，**并并入左右连接键的敏感内容来源** |

要点：筛选引用敏感列、或连接键带敏感来源时，敏感来源影响结果的**每一行**；之后的投影、
常量替换都无法消除；连接时两侧行影响取并集。

### 整次请求拒绝（HTTP 400）

出现以下情况拒绝整次请求，不给出部分审查结果：

* 节点 ID 重复（`DUPLICATE_NODE`）；
* 列名冲突：source 列名、projection 输出列名、filter 引用名、join 输出别名在各自节点内重复（`COLUMN_CONFLICT`）；
* 无效引用：输入节点/被复制列/筛选列/连接键/连接输出列/导出节点不存在（`INVALID_REFERENCE`）；
* 依赖图含环，包括自环（`CYCLE`）。

请求体不符合 JSON Schema（未知字段、同时写 `copy` 与 `constant`、节点数超过 60 等）返回 HTTP 422。

## 单条命令启动

> 端口：**8080**

```bash
docker compose up --build
```

后台运行：

```bash
docker compose up --build -d
```

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## HTTP 调用示例

### 请求

```
POST /api/review
Content-Type: application/json
```

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {
      "id": "users",
      "type": "source",
      "columns": [
        {"name": "uid", "sensitive": false},
        {"name": "email", "sensitive": true},
        {"name": "country", "sensitive": false}
      ]
    },
    {
      "id": "only_cn",
      "type": "filter",
      "input": "users",
      "references": ["country"]
    },
    {
      "id": "export_users",
      "type": "projection",
      "input": "only_cn",
      "columns": [
        {"name": "user_id", "copy": "uid"},
        {"name": "mail", "copy": "email"},
        {"name": "bucket", "constant": "ALL"}
      ]
    }
  ],
  "export": "export_users"
}'
```

也可以直接用仓库里的示例：

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  --data @examples/request.json
```

### 响应

```json
{
  "export": "export_users",
  "blocked": true,
  "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {
      "name": "mail",
      "blocked": true,
      "content_reasons": [{"node_id": "users", "column": "email"}],
      "row_reasons": []
    },
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}
  ]
}
```

### 行影响示例：筛选敏感列后，连常量列也被阻断

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "id_card", "sensitive": true}, {"name": "score"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
    {"id": "e", "type": "projection", "input": "f",
     "columns": [{"name": "score", "copy": "score"}, {"name": "lit", "constant": 1}]}
  ],
  "export": "e"
}'
```

`score`（非敏感复制列）与 `lit`（常量列）的 `row_reasons` 都包含
`{"node_id":"s","column":"id_card"}`，均被阻断。

### 连接示例：敏感连接键影响连接结果每一行

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "orders", "type": "source",
     "columns": [{"name": "uid_enc", "sensitive": true}, {"name": "amount"}]},
    {"id": "users", "type": "source",
     "columns": [{"name": "uid"}, {"name": "name"}]},
    {"id": "j", "type": "join",
     "left_input": "orders", "right_input": "users",
     "left_key": "uid_enc", "right_key": "uid",
     "outputs": [
       {"alias": "amt", "side": "left", "column": "amount"},
       {"alias": "uname", "side": "right", "column": "name"}
     ]}
  ],
  "export": "j"
}'
```

两个输出列本身均不含敏感内容，但都因敏感连接键 `orders.uid_enc` 的行影响而阻断。

## 把审查结论修订为可再次提交的安全图：`POST /api/revise`

`/api/revise` 在**完全相同的图校验规则**下，尝试把被阻断的导出图修订为一张可直接再交给
`/api/review` 且判为安全的新图。请求体在 `nodes` / `export` 之外增加一个字段：

* `allowed_columns`：调用方**允许被替换**的导出列名列表，列表内列名必须**互异且存在**
  于导出节点上（否则按整次请求错误处理：重复为 `COLUMN_CONFLICT`，不存在为
  `INVALID_REFERENCE`，HTTP 400）。可以为空列表 `[]`，表示不允许替换任何列。

修订规则（服务端不会做除此以外的任何修改）：

* 原图已经安全：**原样返回** `nodes`，不新增节点，`export` 不变，`replaced_columns` 为 `[]`；
* 否则只允许在导出节点**末尾追加一个 projection**：保持列名、列顺序与行集合不变；
  对“值带有敏感内容来源、且在允许列表中”的列写入常量 `null`，其余列按同名 `copy` 复制；
  **原节点一个都不改动**；
* 出现以下任一情况则返回 `revisable: false` 及具体 `reasons`，**不返回部分改好的图**：
  * `ROW_INFLUENCE`：导出关系存在行影响（投影/置空无法消除行影响，这是结构性不可修订）；
  * `BLOCKED_COLUMN_NOT_ALLOWED`：仍有带敏感内容来源的阻断列不在 `allowed_columns` 中
    （响应 `columns` 给出这些列名）；
  * `NODE_LIMIT`：追加这一个投影后节点数会超过 60。

成功响应包含完整的 `nodes`、更新后的 `export`（新投影节点 ID，形如 `revise_null_0`，
保证不与原节点冲突）、实际被置空的列 `replaced_columns`，以及按现有规则对新图**重新审查**
得到的 `review`（其中 `blocked` 必为 `false`）。把返回的 `nodes` 与 `export` 原样 POST 到
`/api/review` 即可得到同样的安全结论。

### 可执行的修订 → 复审调用

先修订（只允许把 `mail` 一列置空）：

```bash
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "users", "type": "source",
     "columns": [
       {"name": "uid", "sensitive": false},
       {"name": "email", "sensitive": true},
       {"name": "country", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users", "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn",
     "columns": [
       {"name": "user_id", "copy": "uid"},
       {"name": "mail", "copy": "email"},
       {"name": "bucket", "constant": "ALL"}]}
  ],
  "export": "export_users",
  "allowed_columns": ["mail"]
}'
```

响应：

```json
{
  "revisable": true,
  "nodes": [
    {"id": "users", "type": "source",
     "columns": [
       {"name": "uid", "sensitive": false},
       {"name": "email", "sensitive": true},
       {"name": "country", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users", "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn",
     "columns": [
       {"name": "user_id", "copy": "uid"},
       {"name": "mail", "copy": "email"},
       {"name": "bucket", "constant": "ALL"}]},
    {"id": "revise_null_0", "type": "projection", "input": "export_users",
     "columns": [
       {"name": "user_id", "copy": "user_id"},
       {"name": "mail", "constant": null},
       {"name": "bucket", "copy": "bucket"}]}
  ],
  "export": "revise_null_0",
  "replaced_columns": ["mail"],
  "review": {
    "export": "revise_null_0",
    "blocked": false,
    "columns": [
      {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
      {"name": "mail", "blocked": false, "content_reasons": [], "row_reasons": []},
      {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}
    ]
  }
}
```

再把返回的图直接交给原审查接口复审（有 `jq` 时）：

```bash
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  --data @examples/revise_request.json \
  | jq '{nodes, export}' \
  | curl -s -X POST http://localhost:8080/api/review \
      -H 'Content-Type: application/json' --data @-
```

没有 `jq` 时，用 Python 标准库即可（无需安装任何东西）：

```bash
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  --data @examples/revise_request.json -o /tmp/revised.json

python3 -c 'import json,urllib.request; r=json.load(open("/tmp/revised.json")); \
req=urllib.request.Request("http://localhost:8080/api/review", \
data=json.dumps({"nodes": r["nodes"], "export": r["export"]}).encode(), \
headers={"Content-Type":"application/json"}); print(urllib.request.urlopen(req).read().decode())'
```

两种方式都会打印 `"blocked": false` 的复审结果。

### 不可修订示例（行影响）

上面“筛选敏感列”的图即使允许替换所有列，也无法靠追加投影消除行影响：

```bash
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "id_card", "sensitive": true}, {"name": "score"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
    {"id": "e", "type": "projection", "input": "f",
     "columns": [{"name": "score", "copy": "score"}, {"name": "lit", "constant": 1}]}
  ],
  "export": "e",
  "allowed_columns": ["score", "lit"]
}'
```

```json
{
  "revisable": false,
  "reasons": [
    {
      "code": "ROW_INFLUENCE",
      "message": "the export relation is row-tainted by sensitive columns; a column projection cannot remove row-level influence",
      "row_reasons": [{"node_id": "s", "column": "id_card"}]
    }
  ]
}
```

## 节点 JSON 结构

```jsonc
// source
{"id": "s", "type": "source",
 "columns": [{"name": "col", "sensitive": true}]}

// projection：copy 与 constant 二选一（constant 允许为 null）
{"id": "p", "type": "projection", "input": "s",
 "columns": [{"name": "out", "copy": "col"}]}

// filter：保留全部输入列，references 为条件引用到的列
{"id": "f", "type": "filter", "input": "s", "references": ["col"]}

// join：等值连接；outputs 别名必须互异，side 为 "left" 或 "right"
{"id": "j", "type": "join",
 "left_input": "a", "right_input": "b",
 "left_key": "ka", "right_key": "kb",
 "outputs": [{"alias": "x", "side": "left", "column": "v"}]}
```

* 节点可以乱序给出，服务按依赖做拓扑排序；只审查导出节点可达的祖先链。
* 一个关系连到自身两侧的等值连接（左右输入相同）是合法的；自环依赖（节点输入是自己）按环拒绝。
* `content_reasons` / `row_reasons` 中的来源按 `(node_id, column)` 排序，保证输出确定。

## 开发与测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```

## 文件说明

```
app/models.py     请求模型与结构约束（Pydantic）
app/engine.py     图校验、拓扑排序、来源传播、逐列审查与安全修订
app/main.py       FastAPI 入口（POST /api/review，POST /api/revise，GET /health）
examples/         请求示例（request.json、revise_request.json）
tests/            传播规则、拒绝场景与修订场景测试
Dockerfile        容器镜像（python:3.11-slim，暴露 8080）
docker-compose.yml 单命令编排
```
