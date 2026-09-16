# 分割标注工作台

图像分割团队的标注协作系统：React 前端 + FastAPI 后端 + OpenCV 掩膜处理 + PostgreSQL 版本存储。

## 核心规则

| 场景 | 行为 |
|---|---|
| 标注者修边 | 画布上涂抹/擦除掩膜，保存时必须带 `base_version` |
| 多人改同一区域 | 像素级冲突检测：双方 diff 的交集非空 → `409` + 冲突区域，**不写库**；只能显式 `resolution=overwrite` 或放弃重载 |
| 复核退回 | 复核者圈选多边形区域退回，区域存库；标注者改到该区域像素后重新提交才自动解除，没碰的区域保持打开 |
| 并发复核 | 行锁 + `expected_version` 乐观检查，同时通过/退回只有一个成功，其余 `409` |
| 双盲仲裁 | 负责人发起后系统为双方各建隔离标注，并为每侧签发随机**访问令牌**（仅创建时返回一次，由负责人分发）：OPEN 期间读取/保存/提交该侧都必须持令牌（姓名不是凭证），双方提交后自动计算 XOR 差异连通域并解除隔离，仲裁者逐区域裁定 A/B，合并生成正式掩膜版本（`source=arbitration`）直接进复核 |
| 原图替换 | 旧标注全部变 `stale`，必须显式 **迁移**（掩膜搬到新版本，尺寸变了会最近邻缩放）或 **作废**，系统不自动处理；未完成的仲裁（open/arbitrating）同时置 `void`，裁定记录保留 |
| 训练集导出 | 创建任务时冻结 `(annotation, version)` 快照，后台逐条导出；单 runner 认领（行锁+令牌+心跳租约），崩溃任务启动时自动恢复为可重试；条目状态与进度同事务提交；输出幂等（重试不产生重复行） |

## 目录

```
backend/
  app/
    main.py              FastAPI 路由
    models.py            SQLAlchemy 模型（版本、退回区域、仲裁、导出任务）
    services/masks.py    OpenCV 掩膜原语（diff/交集/连通域/多边形栅格化）
    services/annotations.py  保存掩膜 + 冲突检测 + 盲标隔离检查
    services/review.py   提交/通过/退回
    services/arbitration.py  双盲发起/提交/差异比对/裁定合并
    services/images.py   上传/替换/迁移/作废（替换时失效未完成仲裁）
    services/exports.py  冻结导出 + 断点重试
  tests/                 pytest：并发冲突、复核流程、图片替换、双盲仲裁、导出重试
frontend/                React + Vite（纯 canvas 编辑器）
```

## 启动

```bash
# 1. PostgreSQL（本仓库用 micromamba 装的本地实例）
./scripts/start_db.sh          # 首次会 initdb + 建库建用户

# 2. 后端
cd backend && ../.venv/bin/uvicorn app.main:app --reload --port 8000

# 3. 前端
cd frontend && npm run dev     # http://localhost:5173，/api 代理到 8000
```

## 测试

```bash
cd backend && ../.venv/bin/python -m pytest tests/ -v
```

覆盖：并发修边冲突（不静默覆盖）、并发复核只有一个赢家、退回区域跟踪、
原图替换后的迁移/作废、导出冻结版本、导出中途失败后的断点重试、并发导出互斥。

## 主要 API

```
POST   /images?name=                     上传图片
POST   /images/{id}/replace              替换原图（旧标注 → stale，未完成仲裁 → void）
PUT    /annotations/{id}/mask            保存掩膜（author + base_version，409 返回冲突区域）
POST   /annotations/{id}/submit          提交复核
POST   /annotations/{id}/review/approve  通过
POST   /annotations/{id}/review/reject   退回指定区域（多边形）
POST   /annotations/{id}/migrate         显式迁移到新原图
POST   /annotations/{id}/invalidate      显式作废
POST   /arbitrations                     发起双盲仲裁（响应一次性返回两侧访问令牌）
GET    /arbitrations/{id}                仲裁详情（进度、差异区域、裁定记录；不含令牌）
GET    /arbitrations/{id}/mask?side=a    查看某侧掩膜（OPEN 期间需 ?token= 本侧令牌）
POST   /arbitrations/{id}/submit         一侧提交（需该侧令牌，提交后冻结）
POST   /arbitrations/{id}/adjudicate     逐区域裁定，生成正式掩膜版本进复核
POST   /exports                          冻结版本，后台导出
POST   /exports/{id}/retry               失败重试（断点续跑）
```

已有部署升级：新表由应用启动自动创建，`annotations` 新列需执行一次
`scripts/migrate_20260916_arbitration.sql`（幂等）。
