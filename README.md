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
