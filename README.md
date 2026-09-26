# 增加安置片区施工变更影响审批基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `src/construction_change/`：安置片区施工变更影响审批、公共服务版本、楼栋清单、约束剩余量与冲突申请、整体审批、现场回执与回退；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
PYTHONPATH=src python3 -m construction_change.acceptance --workspace .
```

四条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、危房测量分析、改造审批和施工变更影响审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m construction_change.api --database construction.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。

## 施工变更影响审批

`construction_change` 服务面向安置片区施工调整，覆盖住房套数、给排水、学位、道路承载和消防覆盖五项约束：

- 公共服务版本按片区保存容量快照并逐版链接，楼栋清单随变更修订一并存档；
- 提交变更时依据当前设施快照计算每项约束的剩余量，并按优先级逆序列出被排斥的冲突申请；
- 施工窗口、资源冻结和回退方案作为整体由他人审批，批准后冻结在窗口内对新申请生效、冲突申请被排斥；
- 现场回执按步骤顺序推进，失败时只能转入明确回退或人工接管，部分完成不会被标记为成功；
- 每次修订保留与上一版的字段级差异链，`GET /changes/{id}/revisions` 可查；
- `GET /changes/{id}/impact` 与 `GET /changes/{id}/explanation` 解释每项决定的快照依据、排除规则和审批捆绑摘要。
