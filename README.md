# dataset-conform

一个 AI Agent skill：以一份「参考样例」为格式与质量基准，对整份结构化数据集做**全量体检、批量对齐修复和缺失补全**。

数据领域无关——英语词根、题库、商品目录、知识卡片、法条摘要都适用。领域知识来自你提供的参考样例与规则文档，不来自本 skill。

## 解决什么问题

几 MB 的数据集塞不进模型上下文，直接交给模型全文改写会丢条目、改坏格式、编造内容。本 skill 的做法是把确定性工作交给脚本，语义判断交给模型：

1. 从参考样例（以及已有的规则文档）提炼出机器可校验的 `spec.json` + 人可读的 `spec.md`，再拿样例校准：样例报出 error 时先判断是 spec 太严还是样例本身有缺陷——同类条目**全都**违反是规则漏了一种数据形态，只有**个别**条目违反则是样例自身的缺陷
2. 脚本全量体检，定位所有机械性问题（字段缺失、数组基数不符、索引越界、跨字段约束违反）
3. 语义问题优先派并行子代理**全量**审核，而非自己抽样顺读——抽样几乎必然漏整批
4. 按条目分片，只分有问题的条目，派并行子代理修复
5. 脚本合并 + 完整性守卫（条目守恒、id 不漂移、分片不缺失）+ 复校 + 生成可审阅的 diff

## 三种运行模式

- **audit** — 只出质量体检报告，不改数据
- **conform** — 体检 + 全量修复 + 补全，产出对齐后的数据集与变更报告（默认）
- **targeted** — 只处理限定范围：某类问题、某些字段或某批条目 id，范围外一个字不动

## 安装

把 `dataset-conform/` 目录放进你的 skills 目录即可：

```bash
git clone https://github.com/bymb888/dataset-conform-skills.git
cp -r dataset-conform-skills/dataset-conform <你的项目>/.claude/skills/
```

## 目录结构

```
dataset-conform/
├── SKILL.md                              主流程（7 步）、运行环境与常见坑
├── references/
│   ├── spec-authoring.md                 如何从参考样例提炼 spec.json / spec.md
│   ├── agent-playbook.md                 子代理提示词模板、并发与幂等、验收标准
│   └── examples/word-roots.spec.json     完整 spec 示例（英语词根数据集）
└── scripts/dataset_tool.py               零依赖 CLI：audit / split / merge / diff
```

## CLI

`scripts/dataset_tool.py` 只依赖 Python 3.10+ 标准库，也可脱离 Agent 单独使用：

```bash
# 体检：按 spec 全量校验，输出 issues 与聚合摘要
python dataset_tool.py audit --data data.json --spec spec.json --out audit.json --report audit.md

# 分片：只切出有问题的条目，每片 8 条
python dataset_tool.py split --data data.json --audit audit.json --out-dir shards --size 8 --only-dirty

# 合并：带完整性守卫（条目数、id、index、分片缺失）
python dataset_tool.py merge --data data.json --fixed-dir fixed --out merged.json --spec spec.json --report merge-report.md

# 对比：生成可审阅的变更报告
python dataset_tool.py diff --before data.json --after merged.json --id-field id --out diff.md
```

支持的容器形式：JSON 数组、JSONL、对象映射（`--container array|jsonl|object_map`）。

Windows 上把 `python` 换成 `py -X utf8`：`python` 常指向 WindowsApps 占位 stub（静默失败、零输出），而 `-X utf8` 用来避免中文数据在 GBK 控制台乱码。


`spec.json` 支持的校验类型见 [spec-authoring.md](dataset-conform/references/spec-authoring.md)：类型/必填/空值/长度/正则/枚举/数组基数，以及 `index_in_range`、`value_in_set_from`、`any_in_set_from`、`unique_local`、`unique_global`、`must_contain` 等跨字段检查。

## 设计原则

- **不擅自覆盖原数据**：默认交付 `merged.json` + 报告，用户确认后再替换；原地写回前先备份，并保持原文件的缩进与换行风格
- **最小改动**：只修检出的问题和明确违反语义规则的地方，不顺手「优化」文风——用户要审 diff
- **补全不等于编造**：无法核实的内容宁可留空并在报告中列出，也不生成看起来合理的假数据
- **替换或新增样本时典型性不得低于存量**：不能用"技术上合规"的冷僻替代品换掉不合规的常用样本，常用度、篇幅、复杂度都不该退化
- **不擅自增删条目**：新增条目属于扩充数据集，不属于对齐
