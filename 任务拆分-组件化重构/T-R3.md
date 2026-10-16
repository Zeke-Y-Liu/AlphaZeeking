# 组件化重构-T3 · engines 配置注入 + 注册表版本绑定

## 目标
引擎配置（参数/启停/来源）注入机制 + 配置与注册表版本的一致性绑定。

## 核心交付
1. **配置注入通道**：配置（yaml/db）→ 包装器运行时注入，引擎无硬编码参数
2. **版本绑定**：引擎配置 ↔ registry 版本一致性检查（消费前核对，版本不符→显式告警）
3. **版本不符观察登记**（诚实原则）：已知「配置注入注册表版本不符，结果正确产出」场景——
   显式登记为遗留观察，不阻断、不静默（引擎C 总览同条登记，双向引用）

## DoD 验收
- [x] 注入单测：配置变更可生效、无配置走默认并告警
- [x] 版本绑定校验单测：相符 / 不符两分支
- [x] 不符场景测试：告警可见、评估结果仍正确产出、RUNBOOK 登记处置口径

## 本会话实测（2026-10-11，批次 2 实施记录）

| 交付 | 实测结果 |
| --- | --- |
| 配置注入通道 | [base.py](file:///workspace/alphapocket/applib/engines/base.py) `__call__(args, config=None)`：`config={params,enabled,registry_version}` 经包装层注入（红线：不绕过契约，校验/位范围/provenance 全程生效）；显式 args > config.params > 函数签名默认；enabled=False → `status=disabled` 不进公式（spy 零调用证明）+ [config.py](file:///workspace/alphapocket/applib/engines/config.py) `load_engine_config`（缺文件 {} 不炸/非法形态显式 raise）+ `engine_config_for`（缺条目 {} → 默认+_config_warnings） |
| 版本绑定 | `ENGINE_REGISTRY_VERSION="v1"`（base.py 常量，M1 注册中心接管后由 registry 提供）；不符 → `_version_warning` 显式登记（**不静默、不阻断**，文本指向 RUNBOOK §10） |
| 单测 | [test_engines_config.py](file:///workspace/alphapocket/tests/test_engines_config.py) 6 用例：配置变更真实生效（win=10 vs 默认 20 输出 diff）/ 显式入参覆盖配置 / 停用零调用 / 版本相符无告警+不符告警可见且 state 正确产出 / 缺配置走默认+告警 / 配置文件加载四分支 |
| 过程修复 | 3 处测试侧修正（实现零改动）：签名默认值不出现在 kwargs（`.get("win", 20)`）；成功输出无顶层 status（走 `_engine.status`） |
| RUNBOOK | 新增 **§10 处置口径**：配置位置/注入顺序/版本绑定语义/不符三步处置（核对→bump→重跑）/停用消费方须知；与引擎C 00-总览 §8 遗留观察双向引用 |
| 回归 | α **99 passed**（93+6）零失败 |

## 红线
版本不符必须显式登记不得静默吞掉；配置注入不得绕过包装层契约（T-R2）。
