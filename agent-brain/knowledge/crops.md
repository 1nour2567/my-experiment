# 作物知识（GDD = 生长度日，不是天数）

## 春季 (Spring)
| 作物 | key | GDD | 买入 | 卖出 | 科 | 多收 | 品种 |
|------|-----|-----|------|------|-----|------|------|
| 防风草 | parsnip | 24 | 20 | 50 | root | - | early(18GDD/35G), standard(24/50), giant(36/80) |
| 土豆 | potato | 36 | 40 | 100 | root | - | early(24/70), standard(36/100), sweet(48/160) |
| 花椰菜 | cauliflower | 48 | 60 | 200 | brassica | - | early(36/140), standard(48/200), purple(60/300) |
| 草莓 | strawberry | 36 | 80 | 150 | berry | ✓ | wild(24/100), standard(36/150), white(48/240) |
| 郁金香 | tulip | 36 | 25 | 80 | flower | - | red(30/70), standard(36/80), black(48/150) |

## 夏季 (Summer)
| 作物 | key | GDD | 买入 | 卖出 | 科 | 多收 | 品种 |
|------|-----|-----|------|------|-----|------|------|
| 番茄 | tomato | 54 | 50 | 130 | vine | ✓ | cherry(42/90), standard(54/130), beefsteak(66/200) |
| 玉米 | corn | 60 | 100 | 150 | grain | - | early(48/110), standard(60/150), popcorn(72/240) |
| 蓝莓 | blueberry | 90 | 60 | 100 | berry | ✓ | wild(72/70), standard(90/100), giant(120/180) |
| 甜瓜 | melon | 90 | 60 | 140 | vine | - | early(72/100), standard(90/140), honey(120/220) |
| 大豆 | soybean | 42 | 30 | 90 | legume | - | early(30/60), standard(42/90), edamame(54/140) |
| 小麦 | wheat | 30 | 25 | 70 | grain | - | early(24/50), standard(30/70), durum(42/120) |

## 秋季 (Fall)
| 作物 | key | GDD | 买入 | 卖出 | 科 | 多收 | 品种 |
|------|-----|-----|------|------|-----|------|------|
| 南瓜 | pumpkin | 50 | 100 | 320 | vine | - | small(36/200), standard(50/320), giant(72/500) |
| 小麦 | wheat | 30 | 25 | 70 | grain | - | (同上) |
| 玉米 | corn | 60 | 100 | 150 | grain | - | (同上) |

## 冬季 (Winter)
| 作物 | key | GDD | 买入 | 卖出 | 科 | 多收 | 品种 |
|------|-----|-----|------|------|-----|------|------|
| 冬季种子 | winter_seeds | 15 | 56 | 80 | root | - | standard only |
| 粉末瓜 | powder_melon | 40 | 70 | 180 | vine | - | standard(40/180), giant(60/300) |

## 全季通用
粉末瓜 (powder_melon) 四季可种。

## 关键知识
- GDD 不是天数！每天积累量受天气、温度、土壤影响
- 多收作物 (✓): 收获后不消失，GDD 重置到 25%，继续生长
- 品种选择: plant 时传 variety 参数，珍品卖价更高但 GDD 需求也更高
- 大豆固氮 (+N)，改善后续作物土壤
- 连作惩罚: 同一地块连续种同作物 → 产量递减
- 轮作奖励: 换不同科作物 → +15% 产量
