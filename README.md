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
bin/bidfact.py      CLI
tests/              unittest 套件、夹具生成器、手工预期、大样本内存样例
fixtures/           可提交的小夹具（含 expected_walkthrough.json）
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
