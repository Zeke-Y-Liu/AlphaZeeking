# T-L5 · 巡检闭环：假设复查 + active_pool 自动续约 + 通知

## 目标
巡检结果的下游动作闭环：假设登记复查、active 池自动续约/摘除、降级通知。

## 核心交付
1. **review_items_fl（假设仍成立吗复查步，R3 纪律）**：
   - `weakening/invalid` 因子 → 自动带出其 hypothesis 生成**待答复查项**
   - `healthy` 因子 → 按 20% 抽查生成复查项
   - 复查项落 `review_items_fl`（登记表），答复状态可跟踪
2. **active_pool 自动续约**：
   - 每次（季度）巡检后重算 active_pool：healthy/weakening 保留，**invalid 自动摘除**
   - 摘除动作留痕（摘除时间/巡检证据/影响面引用清单——消费 T-L3 fl_refs）
3. **通知**：weakening 标警 + 策略层提示（哪些策略引用了 weakening 因子）；invalid 摘除通知（引用方收到降级提示）
4. **降级演练基准**（旧会话实测）：review 必备 4、pool 摘除 1（`ops_review_fl`）

## DoD 验收
- [ ] 摘除演练：invalid 因子 → active_pool 自动摘除 + 影响面清单 + 通知三链全通
- [ ] 复查项生成：weakening/invalid 带出 hypothesis 待答项；healthy 20% 抽样确定性（seed）
- [ ] 全量回归零失败

## 依赖与红线
- 依赖：T-L4（fl_factor_health 判定）；T-L3（fl_refs 影响面）；R3 hypothesis 登记表
- 红线：摘除必须留全痕（证据+影响面+时间）；「假设仍成立吗」未答项不得静默清除；invalid 摘除是自动的，但 deprecated（退役）仍是人工决策
