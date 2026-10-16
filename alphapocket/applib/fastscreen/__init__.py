"""M7 T-F8：三引擎交叉核对（分支 A）——vectorbt 快筛 + Qlib 交叉 × M6 自研精验。

契约红线：
- 新依赖隔离：vectorbt / qlib 只允许在本包（applib/fastscreen/）内 import，
  主链（backtest / factor_backtest / strategy* / report 等）零新 import；
- 信号侧复用主链同口径截面合成（composite_scores / composite_multi_meta），
  禁止另起炉灶重写打分逻辑；
- 快筛是排序工具不是精验数值：无 T+1 / 无涨跌停 / 无整手，网格最优参数须回
  自研精验通道复核后才能采纳。
"""
