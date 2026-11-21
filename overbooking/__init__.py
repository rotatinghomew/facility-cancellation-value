"""共用场地候补与限量超发的评估、规则确认与封账诊断。

模块划分：

* ``model``     —— 资料装载、校验与资源组合模型
* ``timeline``  —— 按值班可知时刻（received）组织的事件时间线
* ``risk``      —— 只从历史营业日学习的爽约风险与释放前置时间
* ``policy``    —— 按单放候补、限量超发、不超发三种策略
* ``engine``    —— 按接受下一单的时刻做回看的逐单仿真
* ``evaluate``  —— 分时段策略比较、适用性判定与联动到场情景
* ``guardrails``—— 每场无法入场与等待代价的底线
* ``planner``   —— 营业日规则确认、候补确认与替代释放
* ``diagnostics``—— 当晚补报后的容量滞留归因
"""

from .model import Dataset, Booking, load_dataset  # noqa: F401
