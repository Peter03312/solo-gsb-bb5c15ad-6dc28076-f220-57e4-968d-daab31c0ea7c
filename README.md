# 实验室样品流转 Web 应用（FastAPI + PostgreSQL）

工作人员登记样品与允许温区，发起含交出人 / 接收人 / 目的地的交接；接收人确认前样品位置保持不变。系统记录温度读数，**读数越界立即隔离样品并使待确认交接失效**；隔离期间拒绝一切转移，只有**注明理由的复核结论**可以解除隔离。所有动作写入只增的审计日志并可导出。

## 一键启动

需要 Docker 与 Docker Compose v2（`docker compose` 命令）。

```bash
docker compose up --build
```

启动后：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/ | 样品总览、登记、异常速览 |
| http://localhost:8000/samples/{id} | 样品当前状态、交接 / 读数 / 复核操作、流转时间线、异常记录 |
| http://localhost:8000/audit | 完整审计记录页面（含导出按钮） |
| http://localhost:8000/docs | 交互式 API 文档（Swagger UI） |
| http://localhost:8000/healthz | 健康检查 |

首次启动时容器会自动等待数据库就绪、创建全部表结构，并写入 4 名初始工作人员和 3 个演示样品（正常 / 隔离 / 待确认交接各一），便于直接体验。停止并清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 同时删除数据库数据卷（全新初始化）
```

## 配置项

通过项目根目录 `.env`（可由 `.env.example` 复制）或环境变量配置，改后重新 `docker compose up`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEB_PORT` | `8000` | Web 服务在宿主机暴露的端口（容器内固定 8000） |
| `DB_PORT` | `55432` | PostgreSQL 在宿主机映射的端口（容器间使用 5432） |
| `POSTGRES_DB` | `labflow` | 数据库名 |
| `POSTGRES_USER` | `lab` | 数据库用户 |
| `POSTGRES_PASSWORD` | `labpass` | 数据库密码（生产环境请务必修改） |
| `SEED_DEMO_DATA` | `true` | 是否写入演示样品；`false` 时只建表并创建初始人员 |

`DATABASE_URL` 已在 Compose 内根据上述变量拼装，一般无需单独修改。

## 业务规则与实现要点

- **交接**：发起时锁定样品行，记录出发位置与目的地；确认前位置不变。接收人确认后位置才更新为目的地；只有指定接收人可确认、交出人可取消。同一样品同一时刻只允许一个待确认交接（数据库部分唯一索引兜底）。
- **温度越界**：记录读数在同一事务内判定；越界则样品立即置为隔离态、开启隔离记录，并把该样品所有待确认交接批量置为“已失效”（位置不变）。
- **隔离管控**：隔离期间发起交接、确认交接一律返回 `409` 并给出明确中文原因。只有复核结论为“复核通过”**且填写了理由**才会解除隔离；“继续隔离”保持禁止转移。
- **读数更正**：更正不覆盖原数据，而是新增一条读数，保留原始值、更正理由与操作者；更正**永远不会自动解除隔离**。
- **并发控制**：
  - 所有状态变更先 `SELECT ... FOR UPDATE` 锁定样品行，交接确认 / 取消先锁交接行再锁样品行，锁顺序统一，避免死锁；
  - 样品带 `version` 字段，页面表单回传当前版本（`expected_version`），基于过期页面的确认 / 读数 / 复核返回 `409`，提示“操作基于过期状态，请刷新后重试”；
  - 部分唯一索引保证并发下“仅一个待确认交接 / 仅一条未解除隔离”；并发确认、并发隔离只有一个事务生效，另一个收到明确冲突提示。
- **时间线与审计**：样品详情页合并交接、读数、隔离、复核、冲突拒绝事件生成流转时间线；`audit_logs` 只增不删，`GET /api/audit` 支持按样品筛选 / 仅看异常，`GET /api/audit/export?fmt=csv|json` 导出完整审计记录。

## API 摘要

```text
POST   /api/staff                                   人员建档
GET    /api/samples                                 样品列表
POST   /api/samples                                 登记样品（编号/温区/位置）
GET    /api/samples/{id}                            样品当前状态
GET    /api/samples/{id}/timeline                   流转时间线
POST   /api/samples/{id}/handovers                  发起交接（交出人/接收人/目的地）
POST   /api/handovers/{id}/confirm?operator_id=..   接收人确认（位置才变更）
POST   /api/handovers/{id}/cancel?operator_id=..    交出人取消
GET    /api/handovers[?sample_id=]                  交接记录
POST   /api/samples/{id}/readings                   记录温度（body 可带 expected_version）
POST   /api/readings/{id}/correct                   更正读数（新值/理由/操作者）
POST   /api/samples/{id}/reviews                    复核结论（release / continue，理由必填）
GET    /api/audit[?sample_id=&exceptions_only=]     审计查询
GET    /api/audit/export?fmt=csv|json               导出完整审计
```

错误响应统一为 `{"error": {"code": "...", "message": "中文原因"}}`，业务拒绝 / 并发冲突使用 HTTP 409。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 指向自建 PostgreSQL，例如：
export DATABASE_URL='postgresql+psycopg2://lab:labpass@localhost:5432/labflow'
python -m app.bootstrap       # 建表 + 初始数据（可重复执行，幂等）
uvicorn app.main:app --reload
```

## 测试

仓库内含端到端测试，使用 FastAPI TestClient 对真实 PostgreSQL 运行，包含多线程并发确认 / 并发隔离场景（58 项断言）：

```bash
PYTHONPATH=. DATABASE_URL='postgresql+psycopg2://lab:labpass@localhost:5432/labflow' \
  python tests/e2e_test.py
```

## 目录结构

```text
app/
  main.py        FastAPI 入口、异常处理
  api.py         JSON API（含审计导出）
  web.py         页面路由
  services.py    业务逻辑：事务、行锁、版本、隔离/交接/复核、时间线
  models.py      SQLAlchemy 模型与约束
  schemas.py     Pydantic 校验
  enums.py       状态枚举与中文标签
  bootstrap.py   建表与初始数据（幂等）
  templates/     Jinja2 页面
  static/        CSS 与前端脚本
Dockerfile  docker-compose.yml  entrypoint.sh  requirements.txt
tests/e2e_test.py
```
