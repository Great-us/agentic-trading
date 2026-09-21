# c2c_a7e2 终审（ChatGPT 6 Pro，2026-09-20）——DONE / APPROVED / OPEN_ITEMS: NONE

> 八轮收敛：9 → 5 → 4 → 2 → 1 → 1 → 1 → 0。原文存档如下。

```
[C2C]
STATE: DONE
TASK_ID: c2c_a7e2
ITERATION: 8
WORKSPACE: Trading
BASELINE: 97a65d6
REVIEW: APPROVED
OPEN_ITEMS: NONE
CLOSED_THIS_ITERATION: R8

最后一项入口检查已修复。P1-A-1～A-4 执行漏斗修复包验收通过；此前关闭项保持关闭，无新增退回项。

R8 关闭依据
- 先检查事件类型，再匹配提交尝试。snapshotOrder() 首先要求 snap.event_kind === "order_submitted"；非提交快照或缺失事件类型直接返回 null，不会因时间戳相同而借用提交结果。真正的提交快照仍可匹配无 order_id 的尝试，并继续要求唯一匹配。（Today.tsx:404–418）
- 下游降级与该入口一致。无法配对的拒绝链快照进入未确定分支，不再回退到链级拒绝；成功配对后的完成、部分成交、拒绝和终态判断仍取自对应尝试。（Today.tsx:467–500）
- 回归覆盖了指定反例。新测试依次写入同一时间戳的拒绝提交和 sizing 否决，先核对聚合选出的最新快照确实为 event_kind="sizing"，再限定 sizing 区域断言不得显示"该次提交被拒绝"，同时保留原尝试的拒绝事实。（test_frontend_render.py:1805–1845）

执行证据：execution_output(id=15) 未截断；P1 847 passed exit 0；65 共享文件一致 risk.yaml 字节一致；TS 检查及 Vite 构建通过；P2/P3/P4 沿用已核验的 682+1skip/997/961（不表述为本轮重跑）。当前 HEAD 仍为 97a65d6，工作树尚未提交。

NEXT_EXPECTED_STEP: 将本轮 DONE 登记到 PROGRESS.md，修复包可进入既定提交流程。按明确文件清单纳入已审阅源码、测试、fixture 和必要台账，避免把其他研究文件或散落文档一并提交。本任务不再要求代码返修或重复测试；策略冻结、订单参数及 paper-only 边界保持不变。
```
