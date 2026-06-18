# 农场状态 — {{date}}

> 最后更新: {{time}}

## 基本信息
- **季节**: {{season}}（{{season_en}}）
- **第几天**: {{day}}
- **天气**: {{weather}}
- **金币**: {{gold}}
- **体力**: {{energy_current}} / {{energy_max}}

## 土地
- 开垦: {{tilled}}
- 已种: {{planted}}
- 空闲: {{empty}}

## 作物
| 类型 | 位置 | 生长阶段 | 已浇水 | 距收获 |
|------|------|---------|--------|--------|
{{#crops}}
| {{crop_type}} | {{position}} | {{stage}} | {{watered}} | {{days_to_harvest}} |
{{/crops}}

{{^crops}}
暂无种植作物。
{{/crops}}

## 背包
{{#inventory}}
- {{key}}: {{count}}
{{/inventory}}

## 任务
{{#quests}}
- [{{completed}}] **{{quest_name}}**: {{description}} → +{{xp}}XP +{{gold}}G
{{/quests}}

## 动物
{{#animals}}
- {{name}}（{{count}}只）→ 产出 {{product_name}}（{{product_price}}G，每 {{product_cycle}} 天）
{{/animals}}
