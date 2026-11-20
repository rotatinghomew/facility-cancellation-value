# 共用场地取消预约的容量收益与补救

这里保存共用场地取消预约的容量收益与补救工作所用的匿名合成记录。文件不包含分析结果或居民个人资料。

## 资料

`samples/` 中每个文件都是独立的 UTF-8 JSON 文档：`requests`、`events`、`rooms`、`assistance`、`decision`。日期是当地日历日期。空值表示没有对应观测或状态资料，不是数值零。

start/end 为预约区间，request_minute 是原请求时刻。events.event_minute 是业务发生时刻，received 是值班可知时刻。活动区的陪同工位按同时占用计；桌可依 shareable 合并。linked_scenarios 给出相互关联的到场组合。replacement 为拟议替代的场地与区间；accepted_replacement 表示已同意，并以 received 为值班员得知时间。

## 运行

使用 Python 3.11，无需第三方依赖或服务。`python3 inspect_samples.py` 逐个读取资料并列出记录数；`python3 -m unittest -v` 检查关键原始记录间的关系。输出只描述原始资料，不是业务比较结果。
