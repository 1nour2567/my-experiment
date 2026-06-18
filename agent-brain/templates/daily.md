# {{date}}

## 摘要
{{summary}}

## 今日决策
{{#decisions}}
- **{{time}}** — [[../decisions/{{decision_file}}|{{decision_summary}}]]
{{/decisions}}

## 今日动作
| 时间 | 动作 | 结果 |
|------|------|------|
{{#actions}}
| {{time}} | {{action}} | {{result}} |
{{/actions}}

## 状态变更
{{#state_changes}}
- {{.}}
{{/state_changes}}

## 遇到的错误
{{#errors}}
- {{.}}
{{/errors}}

## 学到的事
{{#lessons}}
- [[../knowledge/{{knowledge_file}}|{{lesson}}]]
{{/lessons}}

---
[[../state/farm|农场当前状态]] | [[../logs/{{date}}-log|详细日志]]
