# 如何从参考样例提炼 spec

目标：把参考样例里隐含的格式契约变成两份显式产物——`spec.json`（机器校验）和 `spec.md`（人可读，含无法机械校验的语义规则）。

## 提炼步骤

1. **读完参考样例的每一条**。样例通常只有 2~5 条，值得逐字读。只看第一条会漏掉可选字段和取值范围。
2. **对目标数据做字段频次统计**，区分标准字段与非标准字段：

   ```bash
   python - <<'PY'
   import json, collections
   d = json.load(open(r"<数据路径>", encoding="utf-8"))
   c = collections.Counter()
   for x in d: c.update(x.keys())
   print(len(d), c)
   PY
   ```

   Windows / PowerShell 下 heredoc 不可用（`<<'PY'` 会报「重定向运算符后缺少文件规范」），改用 here-string 管道，并显式指定 UTF-8：

   ```powershell
   @'
   import json, collections
   d = json.load(open(r"<数据路径>", encoding="utf-8"))
   c = collections.Counter()
   for x in d: c.update(x.keys())
   print(len(d), c)
   '@ | py -X utf8 -
   ```

   注意 `py` 而非 `python`——Windows 上 `python` 常指向 WindowsApps 占位 stub，会静默失败（exit 1、零输出）。

   出现 100/100 的是标准字段；出现 6/100 的通常是历史残留或某次实验的产物——它们**不在参考样例里**，处置方式要问用户，不要默认删。
3. **逐字段决定四件事**：类型、是否必填、是否允许空值、取值约束（长度 / 正则 / 枚举 / 数组基数）。
4. **找出跨字段约束**。这类规则最容易被忽略，也最容易出错：索引指向另一个数组（`correctAnswer` → `options`）、子字段必须取自父字段的某个变体、数组内元素互不重复。另外别忘了**反向约束**：父字段是否必须涵盖子项实际用到的全部取值（如条目级 `root` 字段须声明 examples 里出现过的每个变体）。正向约束漏掉反向的这一半，未声明的合法取值会长期漏检。
5. **把说不清的规则写进 `semanticRules`**，不要硬塞进机械校验。判断标准：这条规则能否只靠"数一数、比一比、匹配正则"得出结论？不能，就是语义规则。
6. **校准**：对参考样例跑 `audit`。报出 error 时先判断是 spec 太严还是样例本身有缺陷——**结构性冲突**（同类条目全都无法满足）说明 spec 漏了一种数据形态，应放宽或加例外；**个例**（只有个别条目不满足）说明样例自身有缺陷，应记入待修清单而不是放宽规则。两种判断都要写进 `spec.md`。若目标数据自身就是参考样例（如样例被注入 LLM prompt 作少样本范例），这一步退化为同义反复，须改用既有规则文档作真源，或先与用户确认规格。


## spec.json 结构

```json
{
  "name": "数据集名称",
  "container": "array",              // array | jsonl | object_map
  "idField": "id",                   // 用于分片守卫；无稳定 id 可省略
  "forbidExtraFields": true,         // 参考样例没有的字段一律报错
  "entry": { ...schema 节点... },
  "checks": [ ...跨字段检查... ],
  "semanticRules": ["脚本查不了、需要子代理判断的规则"]
}
```

### schema 节点

节点通过 `type` 分派，未列出的键会被忽略：

- `object`：`required`（必填字段名数组）、`fields`（字段名 → 节点）、`forbidExtra`（覆盖全局设置）
- `array`：`minItems`、`maxItems`、`items`（元素节点）
- `string`：默认非空；`allowEmpty: true` 允许空串、`minLen`、`maxLen`、`pattern`（正则，`re.search` 语义）、`enum`
- `integer` / `number`：`min`、`max`
- `boolean`
- 任意节点可加 `nullable: true` 允许 null；`severity`（当前仅对 `minLen` 生效，用于把"偏短"降级成 warn）

可选字段的表达方式：**出现在 `fields` 里但不出现在 `required` 里**。

### checks 支持的类型

- `must_contain` — `path`、`substring`：字符串必须包含某内容（如 explanation 必须含 `→`）
- `regex` — `path`、`pattern`
- `array_len` — `path` + `equals` / `min` / `max`
- `index_in_range` — `path`（整数字段）、`of`（数组字段路径）：索引不越界
- `value_in_set_from` — `path`、`source`、`split`（默认 `/`）、`strip`、`lowercase`（默认 true）、`mode`（`exact` | `lenient`）：目标值必须取自 source 字段拆分出的集合；`lenient` 允许互为子串
- `any_in_set_from` — `itemPath`、`paths`（相对 itemPath 的候选字段列表）、`source` 及上面同名参数：子项的若干候选字段中**至少一个**命中条目级标识的变体集合。用于"某个语素必须出现在拆解的前缀、词根或后缀之一"这类或关系约束

- `unique_local` — `path`：同一条目内不得重复（如一条词根下的例词）
- `unique_global` — `path`：跨条目不得重复（如 id）

每个 check 建议写 `id`（会作为报告里的规则名，取有意义的名字，如 `quiz_answer_in_range`），可选 `severity: "warn"` 表示软约束。

路径语法：`a.b`、`a[].b`、`a[].b[].c`。`[]` 表示遍历数组全部元素；路径不存在的分支自动跳过（缺字段由 `required` 负责报错，不会重复报）。

## spec.md 写什么

给人看的部分，也是派给子代理的核心上下文：

- 每个字段的含义与写法要求（含风格：语气、句长、中英文对应关系）
- 语义规则清单，每条都写清"怎么判断"和"违反时怎么修"
- 校准记录：哪些约束因为参考样例本身不满足而被放宽，理由是什么
- 领域边界：什么内容属于本数据集，什么不属于（防止子代理为凑数编造离题内容）

如果项目里已有规则文档（`rule_description.md` 之类），`spec.md` 就写成"引用 + 差异补充"，别复制一份平行规范出来——两份规范早晚会打架。
