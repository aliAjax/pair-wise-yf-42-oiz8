# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；`pairing`：配对建议；`transfer`：机构和运输记录。
- `quarantine`：兽医开具的隔离单，记录隔离原因（`reason`）和场所（`facility`），并驱动可恢复的协调流程。

## 隔离 / 配对 / 运输协调流程

三条状态机由持久化的“协调项”（`coordination_items` 表）串成一个可恢复流程：

- 兽医提交隔离单时记录原因与场所；同一原因、同一个体只允许存在一张未解除的隔离单。
- 个体被**已批准配对**占用时，配对立即失效（`approved → invalidated`），页面/响应给出失效原因。
- 个体**在途运输**时（`in_transit`），隔离流程暂停并在 `blockages` 中列出受影响对象；已开始的运输不回退。隔离单开放期间，新的发运（`ship`）会被拒绝。
- 运输到达（`arrive`）后暂停的隔离单自动继续；解除隔离后，被失效的配对变为 `reconfirm_required`，需重新确认（`reconfirm`）才能完成。
- 两人并发提交/解除时先到先生效；另一人收到 `409 Conflict`，负载里带最新隔离单和阻塞清单。
- 每个协调项在独立事务中执行：写入失败只把该项标记为 `failed` 并保留；`retry` 只续做未完成项，服务重启时自动 `resume`，已完成项绝不重做。

隔离单状态：`submitted → active / blocked → releasing → released`（`releasing` 表示已请求解除但仍在等待在途运输到达）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（含 `quarantines`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。`quarantine` 仅兽医/管理员可创建。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
  - 隔离单：`release`（解除）、`retry`（续做未完成项）。
  - 配对：`reconfirm`（解除后重新确认）。
- `GET /api/recover`：手动触发一次未完成协调项恢复（服务启动时也会自动执行）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
