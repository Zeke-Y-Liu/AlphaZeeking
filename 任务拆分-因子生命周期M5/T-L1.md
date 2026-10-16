# T-L1 · 元模型：FactorMeta + health 三态 + set_health/comp_version

## 目标
因子生命周期元模型地基：FactorMeta 主键与状态/健康双维字段、可恢复的状态文件、健康读写接口。其余 T-L2~T-L5 全部消费本任务数据模型（**先行下发**）。

## 核心交付
1. **FactorMeta**：`factor_id + version` 主键；`hypothesis_id` 必填（R3 纪律：无来源不激活）
2. **双维字段**：
   - lifecycle 状态机：`draft → active → deprecated`（单向，人工决策）
   - health 三态：`healthy / weakening / invalid`（独立字段，不污染状态机；invalid 不自动 deprecated）
3. **状态文件**（JSON 可恢复）：每因子一份 health 指标记录——24 月滚动窗口 IC/IC_IR、覆盖率、漂移、失效环境标签、最近巡检时间戳；进程崩溃重启后可完整恢复
4. **接口**：`set_health(factor_id, version, state, evidence)` / `comp_version(factor_id)`（版本比对与升级判定）；写入即落盘（WAL 或原子替换）

## DoD 验收
- [ ] 元模型单测：主键唯一、hypothesis_id 缺失拒绝、状态机非法迁移拒绝（draft→deprecated 直跳等）
- [ ] 健康三态与 lifecycle 独立性：invalid 不触发 deprecated
- [ ] 状态文件破坏注入（写一半断电模拟）→ 重启恢复完整
- [ ] 全量回归零失败

## 依赖与红线
- 依赖：M1 registry（FactorMeta 落位）；R3 hypothesis 登记表（hypothesis_id 外键校验）
- 红线：health 写入必须带 evidence（IC 序列/覆盖率等可追溯依据）；状态文件是唯一事实源，禁止内存态先行
