# 茶叶鲜叶拼配溯源采证台

离线采录茶园认证与采摘授权、鲜叶交接与双签称重、拼配、返工与标签。以 SQLite 留存原始事件、七类实体、当前裁定和历史裁定；后到的撤证、称重争议与返工会重算下游结果。跨批次筐号只可分配一次，返工重算来源比例并撤销旧标签。所有样例均为合成数据。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m teablend demo --db demo.sqlite
python -m teablend audit --db demo.sqlite
python -m unittest discover -s tests -v
```

各子系统也可单独采录，分别使用 `garden-import`、`leaf-import`、`blend-import`，共同接受 `--db` 和 `--file`。`fixtures` 中每个入口都有正常和失败样例。演示会按六组样例顺序导入，打印全部业务裁定和从原始证据复算的审计结果。同 ID 同内容幂等重放，同 ID 异内容整批回滚。

边界：仅验证仓内模拟凭据和签名字符串，没有实际茶园认证机构、地理测量设备或称重硬件集成。

## 断网双店超售的可恢复合并

两家门店断网时可能各自凭本地快照（均为 L1=10）签出 O1=8、O2=5，恢复联网后 13>10，超过总仓同一实物批次的 10 千克。本地 `accepted(order, snapshot)` 与 `available(snapshot, label)` 只描述门店当时所见；全局结论由下列模块从签单原件、总仓箱码重量、标签合同资格、补货入库、出库扫描、替代条款与撤销签认重放得到：

- `teablend/order_capture.py`：签单/签认原件的内容哈希、同 ID 同内容幂等重放、同 ID 异内容整批冲突。
- `teablend/inventory.py`：总仓与两店的可证库存、在途、已占用；箱码级分配，出库/退货事件按 ID 去重。
- `teablend/fulfillment.py`：`reconcile(fixture, scenario=...)` 求同一库存版本下的可兑现集，状态为 `accepted`、`awaiting_replenishment`、`revocation_consent_required`、`revoked`；标签/等级/有机硬约束经 `teablend.blend.decide` 判定；只有合同合格且完成到仓签收的补货箱才可抵缺口；`MergeBook` 负责上传重放、版本历史和买方更正通知的旧件回读。

固定两条获授权处置：`scenario="replenishment"`（合格 3kg 到仓签收，O1/O2 均接受，箱码 6+2 与 2+3）与 `scenario="revocation"`（B 客户书面签认撤销 O2，O1 保留 8kg，O2 占用释放并发布更正通知）。合并结果与上传/签单时间顺序无关，同一物理千克在任一计划中至多分配一次。

```powershell
python -B -m pytest -q -p no:cacheprovider
```

