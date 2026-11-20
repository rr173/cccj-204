# bidfact — 离线公告事实加工器

把本地网页快照、已抽取的 PDF/HTML 页文本和带坐标的表格单元，加工成可审计的
**标段级事实**（预算金额/币种、材料截止日期、中标结果单位与金额），并依据业务方
显式给出的 relation 清单维护更正时间线。**不联网、不做 OCR、不调用任何招投标业务系统。**

## 设计底线

| 规则 | 实现位置 |
| --- | --- |
| 同 `document_id` 不同字节 = 资料冲突，拒绝出结果号 | `ingestion.load_manifest` / `load_pages` |
| 每个切片必须满足 `page_text[start:end] == text`，错位只记 issue、绝不作证据 | `ingestion.verify_span`、`load_tables` |
| 转载只通过显式 `same_notice` 关联；标题/公司/项目名近似不合并 | 清单 `same_notice` → `same_notice` 表 |
| 标段作用域由编号 + 清单约束共同确认；不唯一即 `pending`，不猜“全部标段” | `extract._resolve_scope`、`_extract_unpaired_prose` |
| 省略年份只用**本公告发布年份**补齐 | `parse_dates.parse_date/find_date` |
| 万元用 `Decimal` 精确换算；缺币种 = `unknown_currency`，不默认人民币 | `parse_money`、`util.wan_to_yuan` |
| 合并表头/跨页续表必须有结构证据（rowspan/colspan、verified marker） | `extract._table_grid`、`_build_chains`、`_header_for_table` |
| 支持范围外的表达进 `unparsed` 带来源，不静默丢弃 | `extract` + `unparsed` 表 |
| relation 是更正关系唯一权威；发布时间新不自动替代 | `relations.validate_and_apply` |
| `replace` 关旧断言+加新断言；`supplement` 只加不关；`retract` 关且不复活 | `closures` 表 + `timeline.current_facts` |
| 缺目标/范围不唯一/新值未确认 → 该项 pending 且不关旧断言；同公告其他字段照常用 | `relations` 状态机 |
| 未来指向、替代环 = `invalid`（图结构判环，与遍历顺序无关） | `relations._detect_invalid_shapes` |
| 未关闭断言同值 → 一个值+全部出处；异值 → `conflict`，不按时间/站点选 | `timeline.current_facts` |
| 全撤回 `withdrawn`；从未声明 `absent`；待确认材料单列 `pending` | 同上 |
| 三个读取入口分离：当前事实 / 公告自身声明 / 完整时间线 | `timeline` 三个函数 |
| 结果号绑定规则版本+本次资料集合；重跑不重复断言；换规则出新号不改旧号 | `util.fingerprint`、确定性主键、`pipeline` |
| 页文与证据路径不常驻内存，逐文档流式处理，落 SQLite | `pipeline` + `storage` |

## 目录

```
bidfact/            库代码（仅标准库）
  parse_dates.py    ISO/中文日期、省略年份补齐
  parse_money.py    币种、元/万元、表头单位继承、unparsed 识别
  ingestion.py      清单/字节/切片核验，逐文档流式读取
  extract.py        prose + 表格抽取、作用域、续表、合并表头
  relations.py      relation 校验、回指核验、环/未来判定、闭合
  timeline.py       current_facts / document_statements / timeline
  storage.py        SQLite 持久层（确定性主键，重跑幂等）
  pipeline.py       编排 + 结果号
  export.py         facts/conflicts/evidence/relations/statements/timeline
  fund/             采购资金月结（输入、勾兑、分摊、退款、独立资金库、读写、
                    只读公告依据影响分析 impact）
bin/bidfact.py      CLI（含 fund 子命令）
bin/bidfact_fund.py fund 子命令的实现
tests/              unittest 套件、夹具生成器、手工预期、大样本内存样例
fixtures/           可提交的小夹具（含 expected_walkthrough.json、fund/ 月结样例、
                    impact/ 影响分析样例）
```

## 运行

```bash
# 生成小夹具（偏移与 sha256 自动计算，保证切片真实）
python3 -I tests/make_fixtures.py

# 全量测试（标准库 unittest）
python3 -I tests/run_tests.py

# 处理一个资料集合
python3 -I bin/bidfact.py run \
  --collection fixtures/walkthrough \
  --relations fixtures/walkthrough/relations.json \
  --results ./results --export

# 三个读取入口
python3 -I bin/bidfact.py facts    <result_dir> --at 2024-03-15T23:59:59
python3 -I bin/bidfact.py statement <result_dir> --document docB
python3 -I bin/bidfact.py timeline  <result_dir> --section L-01
python3 -I bin/bidfact.py export    <result_dir>
```

## 输入格式

`manifest.json`（每个 document 给不可变 id、站内 notice_id、站点、发布时间、
明确 project/section id、原文件 sha256 摘要）：

```json
{
  "documents": [{
    "document_id": "docA", "notice_id": "N-A", "site": "site1",
    "project_id": "P-77", "section_ids": ["L-01"],
    "published_at": "2024-01-10T09:00:00",
    "source": {"file": "pages/docA.json", "sha256": "..."},
    "tables": [{"file": "tables/T1.json", "sha256": "..."}],
    "same_notice": [{"notice_id": "N-A", "document_id": "docA2", "site": "site2"}]
  }]
}
```

页文本文件：`{"document_id": ..., "pages": [{"page_no": 1, "text": "..."}]}`
表格文件：每个 cell 给 `page_no/row/col/text/start/end/rowspan/colspan`，
跨页续表给 `continuation: "continues_from:<id>"` 与可核验的 `continuation_marker`。

`relations.json`：业务方权威更正清单，逐项给作用公告、动词
（`replace|supplement|retract`）、目标 document/section/fields，以及必须能在
**作用公告自身页文**切出来的 `quote` 回指。

## 支持与不支持的表达

- 日期：`2024-02-15`、`2024/2/1`、`2024年2月15日`、省略年份的`2月15日`
  （只用发布年份）。`下月底`、`月初/中/底`、日期区间 → `unparsed`。
- 金额：`100 万元`、`0.0001 万元`（精确 = 1 元）、`人民币 880,000 元`、
  表头万元下的裸数字；大写金额、约/左右/余、金额区间 → `unparsed`；
  裸数字无币种 → `unknown_currency`。
- 结果单位：保留原字符串，不做企业实体合并。

## 大样本

`tests/run_tests.py::TestMemoryScaling` 运行时生成 100/1000/5000 篇合成公告并
断言峰值 RSS 不随文档量线性增长；一次手工运行的样例记录在
`tests/memory_report.example.json`（50 倍文档量、50 倍断言，峰值仅增约 10 MB）。
可用 `BIDFACT_SKIP_MEMORY=1` 跳过。

---

# 采购资金月结（fund）

`bidfact/fund/` 是独立的月结模块：银行流水与付款/退款凭证先逐笔勾兑，
再把每张付款凭证的**整张金额**按财务批准的方案分到项目/标段，退款按
**原来那次付款**的分摊冲回。资金库是独立 SQLite 文件；公告结果库只读，
只作为“财务选的标段确有历史中标声明”的证据——预算、中标金额、公告时间
都不当作付款，公告抽取/更正规则与现有命令完全不变。

## 格式

一个“月度包”是一个目录（每账套每月一份）：

```
manifest.json                  账套、月份、源文件、控制单、公告结果库指针
bank_flows.jsonl               银行流水
vouchers.jsonl                 付款/退款凭证
clearing.jsonl                 财务确认的清分行
allocation_plan.jsonl          当月批准的付款分摊方案
control_bank.jsonl             银行侧控制单（按账户/币种/方向）
control_voucher.jsonl          凭证侧控制单（按币种/方向）
```

- **JSON Lines**：一行一个 JSON 对象，逐行流式读取；空行跳过，坏行带
  行号报错并继续读后面，绝不静默丢弃。金额一律为正整数的最小货币单位
  （`amount_minor`，分），不接受小数、千分位、负数或浮点。
- 业务主键区分大小写、按 Unicode 码点比较；不联网、不做汇率换算。

`manifest.json`：

```json
{
  "ledger_id": "BOOK-1",
  "month": "2024-01",
  "announcement_result_dir": "../announcement_results/R…",
  "files": {
    "bank_flow": "bank_flows.jsonl",
    "voucher": "vouchers.jsonl",
    "clearing": "clearing.jsonl",
    "allocation_plan": "allocation_plan.jsonl"
  },
  "control_files": {"bank": "control_bank.jsonl",
                    "voucher": "control_voucher.jsonl"}
}
```

行格式（`?` = 可选）：

```
bank_flow   {kind, ledger_id, account_id, bank_flow_id, month, currency,
             direction: outflow|inflow, amount_minor, memo?}
voucher     {kind, ledger_id, voucher_id, month, currency, direction,
             amount_minor, voucher_type: payment|refund,
             payment_voucher_id?   // 仅退款：本账套较早已关账的付款凭证号
             rate?                 // 仅退款：退回比例，精确 "a/b" 或小数
             memo?}
clearing    {kind, ledger_id, account_id, bank_flow_id, voucher_id,
             amount_minor, memo?}
allocation_plan
            {kind, ledger_id, plan_id, voucher_id, project_id, section_id,
             weight, result_id, result_unit_assertion_id, memo?}
control     {kind: "control", side: bank|voucher, account_id?（仅银行侧）,
             currency, direction, count, amount_minor}
```

凭证号在账套内唯一（跨月也不得重复）；同一方案内一个
`(project_id, section_id)` 只能出现一次；退款凭证必须指向**更早关账月份**
的同币种付款凭证，当月付款、退款之退款一律拒绝。

## 勾兑规则

- 清分行同时核销一条流水与一张凭证；一个 `(流水, 凭证)` 最多一条清分行。
  引用不存在的流水/凭证、跨月、跨币种、跨方向、超额，都在试算中带
  源文件与行号报出，并给出差额；**不用别处的余额抵销**。
- 两端分别逐笔核对“已分配额 vs 原金额”，缺口或超额落在具体那一笔
  流水/凭证上。月末总额相等但逐笔有缺口与超额时照样阻断。
- 控制单两侧各自核对：银行侧按 `(账户, 币种, 方向)`、凭证侧按
  `(币种, 方向)` 比较条数与金额；缺分组、多分组、重复分组、条数或
  金额不符分别报错。一条流水拆给三张凭证，银行侧控制数仍只算一次；
  一张凭证对应多条流水，凭证分摊只执行一次。

## 标段分摊与退款

- 付款凭证的**整张金额**按方案权重整数分配：先取整数商，剩余最小单位
  按余数降序逐个补齐，余数同分按 `(project_id, section_id)` 字典序升序。
  允许部分标段分不到最小单位，总和必须精确等于凭证金额。
- 多条流水付一张凭证时，凭证只分摊一次；每条流水按自身清分金额把
  归属的标段金额精确分走（两条各自 1 分的流水付一张 2 分等权凭证 →
  A 1、B 1，而不是每条都舍入成 A 2）。流水与标段的对应关系两端都必须
  对平，差一个最小单位都会阻断。
- 方案行用公告结果库**只读**校验：`result_id` 必须是该结果号，断言必须
  存在、`field=result_unit`、confirmed、未被更正关闭，且项目/标段吻合。
  不要求它是最新声明，不比对供应商名称；断言定位（结果号、文档、断言
  号、原文切片）随分摊行保存。
- 退款沿用**原付款**的分摊：先冲原付款中 `(project_id, section_id)`
  字典序较大的一行，再冲较小的一行，每行只冲历史上尚未冲过的金额；
  不查退款月的方案。同月多笔退款指向同一原付款时按退款凭证号升序应用，
  与文件/清分行读取次序无关。退款总额超过原付款未冲金额即阻断整月，
  不部分入账；比例留有余数也阻断，不把它藏进月度总数。

## 命令

```bash
# 试算：列出全部可定位差异，不消耗退款额度、不写已关期记录
python3 -I bin/bidfact.py fund trial \
  --ledger ./fund_ledger --package fixtures/fund/january

# 关账：原子追加，可重复提交完全相同的输入
python3 -I bin/bidfact.py fund close \
  --ledger ./fund_ledger --package fixtures/fund/january

# 读取
python3 -I bin/bidfact.py fund month --ledger ./fund_ledger --month 2024-01
python3 -I bin/bidfact.py fund cumulative --ledger ./fund_ledger --month 2024-03
python3 -I bin/bidfact.py fund months --ledger ./fund_ledger
```

`python3 -I bin/bidfact_fund.py <command> ...` 亦可直接运行。

- 首期可指定任意起始月份，之后只能顺序关下一自然月。
- 同一月份重复提交完全相同输入 → 复用已关账结果（`reused: true`）；
  输入有任一字节变化 → 拒绝覆盖旧月。两个进程同时关同一月，只有一个
  真正写入，另一个读到 `reused`；成功后从新进程读取只看到一次冲减。
- 关账在单事务内落定：全月流水/凭证/清分、标段分摊（含正负金额与
  原付款引用）、按 `(项目, 标段, 币种)` 的付款额/退款额/净额，以及每笔
  金额的源文件与 1-based 行号、公告断言定位。任一步失败不留半份正式账、
  不推进月份、不消耗退款额度。
- 月结过程使用磁盘暂存 SQLite，整月流水与多对多展开关系不常驻内存；
  源文件与公告结果库全程只读。

## 样例与测试

```bash
python3 -I tests/make_fund_fixtures.py   # 重建 fixtures/fund（含公告结果库）
python3 -I tests/run_fund_tests.py       # 资金月结测试
```

`fixtures/fund/` 是可复跑的离线样例：`january` → `february` → `march` 覆
盖题面 4/5 分、P/Q 两张凭证、一二三月退款；`march_over` 把三月退款改
成 5 分用于阻断演示；`wiring` 是两笔 1 分付一张 2 分等权凭证。每个包
里的 `expected.json` 是**手工算出的期望值**（不是代码产物），测试逐项
比对金额与血缘。动态测试另外制造分组控制单不符、总金额相等但逐笔
缺口/超额、错币种连线、错误公告锚点、并发关账、大月份内存与超过
64 位的整数精度样例。

手工对账（与 `expected.json` 一致）：

| 月份 | 付款 A | 付款 B | 退款 A | 退款 B | 净额 A | 净额 B |
| --- | --- | --- | --- | --- | --- | --- |
| 2024-01 | 7 | 2 | 0 | 0 | 7 | 2 |
| 2024-02 | 0 | 0 | 0 | 1 | 0 | -1 |
| 2024-03 | 0 | 0 | 1 | 1 | -1 | -1 |

P（5 分）等权分 A 3 / B 2，Q（4 分）全给 A；二月退 P 的 1 分先冲 B；
三月再退 2 分先冲 B 剩余的 1 分、再冲 A 1 分——即使三月的财务方案
已改成只含 B。累计到三月：A 净 6、B 净 0。

---

# 公告依据影响分析（fund impact，只读）

财务按关账当时可核实的中标声明完成月结后，公告可能被更正或撤回。
`fund impact` 回答的是**另一个问题**：已关账付款里，哪些正向分摊行
曾经依赖某条断言、涉及多少已付/已冲/未冲款项、该断言在指定的后续
公告结果库与查询截止时刻下变成了什么。它只生成分析结果，绝不改动
已关账记录、退款额度、公告断言或资金库元数据。

## 输入与边界

- 一个**已关账资金库**（`--ledger`）与一个明确指定的**后续公告结果
  库**（`--result`，bidfact 生成的 SQLite 结果，给结果目录或其
  `store.sqlite3` 均可）；两个库都以 SQLite 只读 URI 打开，全程不联网。
- 一个查询截止时刻 `--at`（ISO 时间）：只有 `published_at <= at` 的
  断言与关闭关系参与判断。
- 可选过滤 `--month-from/--month-to/--project/--section`；省略时覆盖
  资金库中全部已关账月份。退款只统计截止月份（`month-to`，缺省为全部
  已关账月份）之前（含当月）已入账的部分。

## 状态口径（每条正向付款分摊行一个状态）

| 状态 | 含义 |
| --- | --- |
| `closed` | 原断言在截止时刻前已被确认的 `replace`/`retract` 关系关闭；证据给出关闭它的公告、关系号、动词与发布时间。撤回其他断言不会复活已关闭断言 |
| `contested` | 原断言仍开放，但同 `(项目, 标段, result_unit)` 作用域在截止时刻存在两个及以上取值不同的开放确认断言；全部不同值与各自出处都列出，不按发布时间选值 |
| `unchanged` | 原断言仍开放、已确认、作用域一致，且该作用域全部开放断言同值 |
| `unverifiable` | 结果号与所开库不符、断言缺失/非 confirmed/作用域不一致/截止时刻尚未发布，或输入库无法读取；必须带具体 reason，缺数据绝不推断为未变化 |

发布时间较新本身不构成替代；公告当前中标金额也从不被当作付款金额。

## 血缘与金额

- 以资金库保存的 `result_id`、`assertion_id`、项目/标段与原付款分摊为
  起点，按 `(关账月份, 账套, 凭证, 分摊行 rank)` 自然键逐行匹配，凭证
  拆分、退款拆分或同名项目都不会合并锚点。
- 退款沿关账时已保存的 `refund_allocations` 冲回明细回溯到原分摊行，
  **不按退款月方案重算**；每行给出原分摊金额、截至所选月份已冲金额、
  未冲金额及完整退款血缘。
- 一条断言被多笔付款引用时，每笔金额分别保留；状态汇总按分摊行汇总，
  不重复计算任何分摊金额。

## 命令与输出

```bash
python3 -I bin/bidfact.py fund impact \
  --ledger ./fund_ledger --result ./later_results/R… \
  --at 2024-05-01T00:00:00 \
  [--month-from 2024-01 --month-to 2024-03 --project P --section A] \
  [--jsonl ./lines.jsonl] [--report-jsonl ./report.jsonl] [--terminal]
```

- 默认输出 JSON 报告（含按状态、按项目/标段的金额汇总，可逐项展开回
  明细）；`--jsonl` 把明细流式落盘（响应不再内嵌 lines），
  `--report-jsonl` 输出“报告头 + 两类汇总 + 明细”的单文件 JSONL，
  `--terminal` 输出稳定排序的中文终端摘要。
- 每行保留输入结果号、截止时刻、来源文件/行号与数据库自然键，供审计
  复跑；相同参数重复运行得到相同业务内容，运行前后两个输入库字节不变。
- 明细经数据库游标按有界批处理流经临时暂存库，不把全部分摊与关系记录
  载入内存；大月结库的有界内存行为由
  `tests/run_impact_tests.py::TestBoundedMemory` 验证（可用
  `BIDFACT_SKIP_MEMORY=1` 跳过）。

## 样例与测试

```bash
python3 -I tests/make_impact_fixtures.py   # 构造隔离小账与两份以上独立公告结果库
python3 -I tests/run_impact_tests.py       # 影响分析测试（含手工预期）
```

`fixtures/impact/` 是可复跑的离线样例：基线库 S0 经现有流水线生成，
S1–S8 是在其副本上按 bidfact 存储 schema 独立构造的后续结果库
（replace、冲突补充、撤回新断言、缺失断言、作用域不一致、非 confirmed、
错误结果号、空库）；`impact_expected.json` 是逐笔手算的期望状态与金额。
大月结库在测试运行时生成，不作为夹具提交。

