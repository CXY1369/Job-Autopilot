# Agent 操作规范手册

本手册定义了 AI Agent 浏览、理解和操作求职申请页面的标准流程。遵循本手册可确保操作的一致性和准确性。

---

## 页面扫描与规划（每个新页面都要执行！）

### 什么时候需要扫描规划？

**每次遇到新页面都要重新扫描规划**，包括：
- 首次进入申请页面
- 点击 Continue/Next 后跳转到新页面
- URL 发生变化
- 页面内容明显变化（如出现新的表单字段）

### 新页面处理流程

```
新页面 → 1. 等待 Simplify 填写 → 2. 扫描空缺字段 → 3. 规划顺序 → 4. 逐步执行
                                                                    ↓
                                              页面跳转 → 回到步骤 1
```

**具体步骤：**

1. **等待 Simplify 自动填写**
   - 观察是否有 "Autofill complete" 提示
   - 等待自动填写完成后再操作

2. **扫描表单区域**
   - 找出所有**空缺的必填字段**（带 * 标记）
   - 记录字段名称和类型（输入框 / autocomplete / checkbox）

3. **在 summary 中列出计划**
   - 列出空缺字段
   - 按从上到下的顺序规划填写

4. **按计划逐步执行**
   - 每步检查上一步是否成功
   - 遇到问题及时调整

### summary 示例

**首次进入页面：**
```
"扫描页面：Simplify 已填写完成。空缺必填项：Location（autocomplete，显示 Start typing...）、Which office（checkbox 多选，无勾选）。
计划：1. 填 Location（type → click 选择）→ 2. 选 office checkbox → 3. 点击 Submit"
```

**跳转到新页面后：**
```
"新页面：补充问题页。Simplify 已填写。空缺必填项：Education（输入框）、Years of experience（下拉框）。
计划：1. 填 Education → 2. 选 experience → 3. 点击 Continue"
```

**执行过程中：**
```
"Location 已填写完成（Dallas, Texas, United States）。继续计划：下一步选 office checkbox。"
```

### 多页面申请流程示意

```
第 1 页（基本信息）
  ├─ Simplify 自动填写 Name、Email 等
  ├─ Agent 扫描：Location 空、Which office 空
  ├─ 执行：填 Location → 选 office → 点 Continue
  └─ 页面跳转 ↓

第 2 页（补充问题）
  ├─ Simplify 自动填写部分字段
  ├─ Agent 扫描：Education 空、Experience 空
  ├─ 执行：填 Education → 填 Experience → 点 Submit
  └─ 页面跳转 ↓

第 3 页（确认页）
  └─ 检测到 "Thank you" → 返回 done
```

---

## 零、页面结构理解（最重要！先读懂页面再操作）

在操作任何字段之前，必须先理解页面的整体结构。这是避免混淆的关键！

### 0.1 区分页面区域

求职申请页面通常有以下区域，必须区分清楚：

| 区域 | 典型位置 | 内容特征 | 是否需要操作 |
|------|----------|----------|------------|
| **职位信息区** | 左侧边栏或页面顶部 | 显示职位名称、公司、工作地点、薪资、部门等 | ❌ **只读，绝对不要操作** |
| **表单区域** | 页面中间或右侧 | 有输入框、placeholder、checkbox、按钮等 | ✓ 需要填写 |
| **提示/错误区** | 表单字段旁边或顶部 | 红色错误提示、必填标记 * | 需要关注并修复 |

### 0.2 如何区分"职位信息"和"表单字段"

**职位信息（只读，不要操作！）的特征：**
- 通常在页面边栏或顶部的卡片/区块中
- 显示格式是 `标签: 值`（如 "Location: Boston, NYC"）
- **没有输入框**、没有 placeholder、不可编辑
- 这是职位本身的属性，描述这个工作在哪里

**表单字段（需要操作）的特征：**
- 有输入框、有 placeholder（如 "Start typing..."、"Type here..."）
- 或者有 checkbox（方框）、radio（圆圈）可点击
- 字段名后面通常有 `*` 表示必填
- 这才是需要你填写的内容

### 0.3 ⚠️ 最容易混淆的情况：多个 "Location"

页面上可能同时出现多个与 "Location" 相关的内容：

```
┌─────────────────────────────────────────────────────────────┐
│  页面左侧（职位信息区）          页面中间（表单区域）        │
│  ┌─────────────────┐           ┌─────────────────────────┐  │
│  │ Location        │           │ Location*               │  │
│  │ Boston, NYC     │  ← 只读！ │ [Start typing...]       │  │ ← 要填写！
│  │                 │           │                         │  │
│  │ Department      │           │ Which office are you    │  │
│  │ Machine Learning│           │ willing to work out of?*│  │
│  │                 │           │ □ Boston (Cambridge)    │  │ ← 要选择！
│  │ Compensation    │           │ □ San Francisco         │  │
│  │ $160K - $280K   │           │ □ Los Angeles           │  │
│  └─────────────────┘           └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

**这三个是完全不同的东西：**

| 内容 | 位置 | 含义 | 你要做什么 |
|------|------|------|-----------|
| `Location: Boston, NYC` | 左侧职位信息 | 这个职位的办公地点 | **不管它！只是信息展示** |
| `Location*` 输入框 | 表单区域 | 问"你现在住哪里" | 填写用户当前居住地 |
| `Which office...` checkbox | 表单区域 | 问"你愿意在哪里工作" | 选择用户愿意去的城市 |

**正确理解：**
- 看到左侧 "Location: Boston, NYC" → 这只是告诉你职位在哪，不影响你填什么
- 表单中的 "Location*" 输入框 → 填用户当前居住地（如 Dallas）
- 表单中的 "Which office" checkbox → 选用户愿意工作的城市（可多选）

**错误理解（绝对禁止！）：**
- ❌ 看到左侧 "Location: Boston, NYC"，就以为要把 Location 字段填成 Boston
- ❌ 把 Location 输入框和 Which office checkbox 混为一谈
- ❌ 认为用户居住地应该和职位地点一致

### 0.4 观察页面的标准步骤

每次收到新截图时，按以下顺序理解页面：

```
第一步：识别页面布局
  ├─ 哪里是职位信息区？（通常左侧/顶部，只读）
  └─ 哪里是表单区域？（有输入框/checkbox，需要操作）

第二步：聚焦表单区域
  ├─ 忽略职位信息区的内容
  └─ 只关注表单字段

第三步：理解每个表单字段
  ├─ 这个字段的标签是什么？
  ├─ 这个字段问的是什么问题？（理解语义）
  ├─ 字段类型是什么？（输入框 / checkbox / 按钮）
  └─ 当前状态？（已填 / 空 / 有错误提示）

第四步：决定下一步操作
  └─ 优先处理：错误提示 > 空的必填字段 > 提交
```

---

## 一、页面浏览流程

每次收到新截图时，按以下顺序浏览页面：

### 1.1 从上到下扫描
1. 先看页面顶部：公司名称、职位标题
2. 中间区域：表单字段（按顺序）
3. 底部区域：提交按钮、错误提示

### 1.2 识别页面类型
- **申请表单页**：有输入框、下拉框、按钮等表单元素
- **确认/感谢页**：显示 "Thank you"、"Application submitted" 等成功信息
- **错误页**：显示红色错误提示、"Missing required field" 等

### 1.3 记录字段状态
对每个可见字段，记录：
- 字段名称（label）
- 当前值（已填/空/错误）
- 字段类型（输入框/下拉框/按钮等）

---

## 二、字段识别规范

### 2.1 输入框类型

| 特征 | 类型 | 使用操作 |
|------|------|----------|
| placeholder 显示 "Type here..." | 普通输入框 | fill |
| placeholder 显示 "Start typing..." | autocomplete | type |
| 有下拉箭头图标 | autocomplete/下拉框 | type 或 click |

### 2.2 按钮类型

| 特征 | 类型 | 使用操作 |
|------|------|----------|
| Yes / No 两个并排按钮 | 单选按钮组 | click("Yes") 或 click("No") |
| 方框可勾选 | checkbox | click(选项文本) |
| 圆圈可选择 | radio | click(选项文本) |
| Submit / Apply 文字 | 提交按钮 | click(按钮文本) |

### 2.3 下拉框类型

| 特征 | 类型 | 使用操作 |
|------|------|----------|
| 原生 select 元素 | 原生下拉框 | click 打开后 click 选项 |
| 输入后出现选项列表 | autocomplete | type 输入后 click 选项 |
| 点击后出现浮层选项 | 自定义下拉框 | click 打开后 click 选项 |

---

## 三、操作选择规则

### 3.1 操作类型对照表

| 场景 | 操作 | selector | value |
|------|------|----------|-------|
| 填写 Name、Email 等普通输入框 | fill | 字段标签 | 内容 |
| 填写 Location 等 autocomplete | type | 字段标签 | 内容 |
| 点击 Yes/No 按钮 | click | "Yes" 或 "No" | - |
| 勾选 checkbox | click | 选项文本 | - |
| 选择下拉选项 | click | 选项文本 | - |
| 点击提交按钮 | click | 按钮文本 | - |

### 3.2 关键规则

1. **Yes/No 问题永远用 click**，selector 填 "Yes" 或 "No"
2. **看到 "Start typing..." 用 type**，不用 fill
3. **下拉选项出现后用 click** 选择正确选项
4. **提交前确认所有必填字段都已正确填写**

### 3.3 ⚠️ autocomplete 字段的两步操作（最常见错误！）

对于 Location 等 autocomplete 字段，**必须完成两步才算填写成功**：

| 步骤 | 操作 | 说明 |
|------|------|------|
| 第一步 | `type(Location, Dallas)` | 输入内容，触发下拉框 |
| 第二步 | `click(Dallas, Texas, United States)` | 选择下拉选项 |

**这两步必须连续完成，中间不能插入其他操作！**

#### ❌ 错误示例（会导致字段为空）

```
步骤 1: type(Location, Dallas)     → 下拉框出现
步骤 2: click(Yes)                 → 跳去操作其他字段！
结果: Location 字段变空，之前输入的内容丢失
```

#### ✅ 正确示例

```
步骤 1: type(Location, Dallas)     → 下拉框出现
步骤 2: click(Dallas, Texas, ...)  → 选择正确选项
结果: Location 字段显示完整地址，填写完成
```

#### 判断 autocomplete 是否完成

| 字段显示内容 | 状态 | 下一步 |
|-------------|------|--------|
| "Start typing..." | 未开始 | 需要 type 输入 |
| 输入框有文字 + 下拉框出现 | 输入中 | **必须 click 选择！** |
| 完整地址（如 "Dallas, Texas, United States"） | 已完成 | 可以跳过 |

#### 🚨 关键规则

**当你看到下拉框/选项列表出现时：**
- ✅ 下一步**必须是 click 选择正确选项**
- ❌ **绝对不能**跳去操作其他字段
- ❌ **绝对不能**点击 Submit

---

## 四、执行后验证

每次操作后，在下一轮截图中验证：

### 4.1 检查操作字段
- 该字段的值是否已更新为预期值？
- 如果是 autocomplete，下拉选项是否出现/已选择？

### 4.2 检查副作用
- 其他字段是否被意外修改？
- 特别注意 Name、Email 等重要字段是否保持正确

### 4.3 验证结果处理
- **正确**：继续下一个操作
- **错误**：立即修正该字段（优先级最高）

---

## 五、错误处理规范

### 5.1 操作失败时

| 失败原因 | 处理方式 |
|----------|----------|
| fill 失败 | 换成 type 尝试 |
| type 失败 | 先 click 激活输入框，再 type |
| click 选项失败 | 可能下拉框未出现，先 type 触发 |
| 定位不到元素 | 检查 selector 是否正确，尝试简化 |

### 5.2 字段值错误时

立即修正，步骤：
1. 识别错误的字段
2. 使用正确的操作清空并重新填写
3. 验证修正结果

### 5.3 什么情况返回 stuck

**只有以下情况才返回 stuck：**
- 需要登录但没有账号
- 出现验证码（CAPTCHA）
- 页面完全无法加载
- 需要付费

**这些情况不是 stuck，要继续尝试：**
- 某个操作失败 → 换方法重试
- 字段填错了 → 修正
- checkbox 选错了 → 重新选择

---

## 六、操作示例

### 示例 1：填写 Location 字段（autocomplete）⚠️ 必须两步！

**页面状态**：Location 字段显示 "Start typing..."

**⚠️ 这是 autocomplete 字段，必须完成两步：type → click**

**第一步操作**：
```json
{
  "status": "continue",
  "summary": "Location 字段为空，显示 Start typing...，需要输入",
  "next_action": {
    "action": "type",
    "element_type": "input",
    "selector": "Location",
    "value": "Dallas",
    "reason": "输入城市名触发下拉框"
  }
}
```

**执行后观察截图**：下拉框出现，显示：
- Dallas, Texas, United States
- Dallas, Georgia, United States
- ...

**第二步操作**（必须！不能跳过！）：
```json
{
  "status": "continue",
  "summary": "下拉框出现，看到 Dallas, Texas 选项，必须点击选择",
  "next_action": {
    "action": "click",
    "element_type": "option",
    "selector": "Dallas, Texas, United States",
    "reason": "选择下拉选项完成 Location 字段填写"
  }
}
```

**执行后验证**：Location 字段显示 "Dallas, Texas, United States" → ✅ 完成

**❌ 错误做法**：
```
type(Location, Dallas) → 下拉框出现 → 直接去点 Submit 或操作其他字段
结果: Location 字段变空！必须先 click 选择！
```

### 示例 2：回答 Yes/No 问题

**页面状态**：显示 "Are you legally authorized to work in the United States?" 问题，下方有 Yes 和 No 两个按钮

**正确操作**：
```
click(Yes)
  → selector 只填 "Yes"，不要填整个问题
```

**错误操作**：
```
fill(Are you legally authorized..., Yes)  ← 错误！会把 Yes 填到别的输入框
```

### 示例 3：勾选工作地点偏好（checkbox 多选）

**页面状态**：显示 "Which office are you willing to work out of?" 问题，下方有多个 checkbox 选项

**⚠️ 这是 checkbox 多选字段，与 Location 输入框完全不同！**
- Location 输入框 → 问"你住哪里" → 填 Dallas
- Which office checkbox → 问"你愿意在哪工作" → 取交集多选

---

#### 🔑 checkbox 多选的核心逻辑：取交集 + 模糊匹配

**第一步：取交集**

```
用户偏好: [Boston, New York, SF, LA, Dallas, Seattle, Austin]
页面选项: [Boston (Cambridge), NYC (Chelsea), LA (Venice), SF, Remote only]
                              ↓
交集 = 用户偏好 ∩ 页面选项
     = [Boston (Cambridge), NYC (Chelsea), LA (Venice), SF]
                              ↓
排除: Remote only（用户偏好里没有）
```

**第二步：模糊匹配**

用户偏好和页面选项的名称可能不完全相同，需要推理判断：

| 用户偏好 | 页面选项 | 是否匹配 |
|---------|---------|---------|
| Boston | Boston (Cambridge) | ✓ 是同一个 |
| New York | NYC (Chelsea) | ✓ 是同一个 |
| New York | New York City (Chelsea) | ✓ 是同一个 |
| SF | San Francisco | ✓ 是同一个 |
| LA | Los Angeles (Venice) | ✓ 是同一个 |
| Dallas | Remote only | ❌ 不是同一个 |

**匹配后使用页面显示的完整名称**：
- 用户偏好是 "Boston" → 页面显示 "Boston (Cambridge)" → 点击 "Boston (Cambridge)"

**第三步：规划全部执行**

交集有 N 个选项，就必须勾选 N 个：

```
交集结果: [Boston (Cambridge), NYC (Chelsea), LA (Venice), SF]
                              ↓
规划: "需选 4 个城市：Boston (Cambridge)、NYC (Chelsea)、LA (Venice)、SF"
                              ↓
执行: click(Boston (Cambridge)) 
    → click(NYC (Chelsea)) 
    → click(LA (Venice)) 
    → click(SF) 
    → 全部完成，继续下一步
```

**⚠️ 重要规则**：
- 规划了 4 个 → 必须选完 4 个
- 不要选一个就认为完成
- 选完所有交集选项后再继续

---

**❌ 禁止选择：** Remote only（用户偏好中没有）

**✅ 正确操作逻辑：**

1. **取交集**：找出"用户偏好"和"页面选项"的交集
2. **模糊匹配**：推理判断名称是否指向同一事物
3. **全部勾选**：交集中的所有选项都要勾选
4. **已勾选的有效选项不要取消**

**⚠️ 禁止的行为：**
- ❌ 选择用户偏好中没有的选项（如 Remote only）
- ❌ 看到有效选项被勾选却去取消它
- ❌ 反复点击同一个 checkbox（会切换勾选状态）
- ❌ 规划了 4 个却只选 1 个就认为完成

**操作示例**：
```
场景 A：页面有 5 个选项，交集有 4 个
  规划: "交集 4 个：Boston (Cambridge)、NYC (Chelsea)、LA (Venice)、SF"
  执行:
    → click(Boston (Cambridge))    ← 第 1 个
    → click(NYC (Chelsea))         ← 第 2 个
    → click(LA (Venice))           ← 第 3 个
    → click(SF)                    ← 第 4 个，全部完成
    → 继续下一步

场景 B：已经勾选了 Boston (Cambridge)
  → 这是有效选择，不要取消！
  → 继续勾选剩余交集选项：NYC、LA、SF
```

### 示例 4：开放式问题（无选项）

**页面状态**：显示 "What are your top skills?" 或 "List your preferred cities" 等问题，只有输入框，没有 checkbox 选项

**⚠️ 这是开放式问题，需要根据用户资料填写多个值**

**处理规则：**
1. 从用户资料中提取相关信息
2. 默认填写 3 个有效值
3. 用逗号分隔

**示例**：

| 问题 | 用户资料相关内容 | 填写值 |
|------|----------------|--------|
| What are your top skills? | Python, PyTorch, ML, DL, SQL... | Python, Machine Learning, Deep Learning |
| List your preferred cities | Boston, NYC, SF, LA, Dallas... | Boston, New York, San Francisco |
| What programming languages? | Python, JavaScript, Go | Python, JavaScript, Go |

**操作示例**：
```json
{
  "status": "continue",
  "summary": "Skills 问题空缺，根据用户资料填 3 个技能",
  "next_action": {
    "action": "fill",
    "element_type": "input",
    "selector": "Skills",
    "value": "Python, Machine Learning, Deep Learning",
    "reason": "从用户资料提取 3 个主要技能"
  }
}
```

**⚠️ 注意事项**：
- 默认填 3 个，除非问题明确要求更多/更少
- 用英文逗号分隔
- 从用户资料中选择最相关的

---

### 示例 5：发现并修正错误

**页面状态**：Name 字段显示 "Yes"（被错误修改）

**操作序列**：
```
步骤 1: 识别错误
  → Name 字段应该是 "Xingyu Chen"，但显示 "Yes"
  → 这是上一步操作的副作用，需要立即修正

步骤 2: fill(Name, Xingyu Chen)
  → 用正确的名字覆盖错误值

步骤 3: 验证 Name 字段显示 "Xingyu Chen"
  → 正确，继续之前未完成的操作
```

### 示例 6：提交申请

**页面状态**：所有字段已正确填写，底部显示 "Submit Application" 按钮

**检查清单**（提交前确认）：
- [ ] Name 正确
- [ ] Email 正确
- [ ] Location 正确
- [ ] 所有必填字段已填写
- [ ] 没有红色错误提示

**操作**：
```
click(Submit Application)
  → 提交申请
```

**提交后**：
```
观察截图，如果看到 "Thank you" 或 "Application submitted"
  → 返回 done
```

---

## 七、用户信息使用规范

### 7.1 位置相关字段（重要！三个不同的东西）

| 你看到的内容 | 在哪里 | 是什么意思 | 你应该怎么做 |
|-------------|--------|-----------|-------------|
| `Location: Boston, NYC` | 左侧职位信息区 | 职位的办公地点 | **不管它！只读信息** |
| `Location*` 输入框 | 表单区域 | 问"你住哪里" | 填用户居住地（Dallas） |
| `Which office...` checkbox | 表单区域 | 问"你愿意在哪工作" | 选配置中的城市（可多选） |

**关键点：**
- 用户住在 Dallas，这和职位在 Boston 没有任何矛盾
- Location 表单字段填的是用户住哪里，不是职位在哪里
- Which office 是问用户愿意去哪些城市工作，可以多选

### 7.2 常用信息速查

填写表单时，从用户配置中获取真实信息，不要编造：
- Name、Email、Phone → 用配置中的值
- Location 表单字段 → 用户当前居住地（Dallas, Texas）
- Which office checkbox → 用户愿意工作的城市（可多选，参考配置）
- Work authorization → 用配置中的状态
- 其他问题 → 根据配置如实回答

---

## 八、决策流程图

```
开始新一轮
    ↓
截图观察
    ↓
检查上一步结果 ─────→ 发现错误 → 修正错误 → 返回截图观察
    ↓ 正确
识别页面类型
    ↓
┌─────────────────┬──────────────────┬─────────────────┐
│   申请表单页    │    确认/感谢页   │     错误页      │
│                 │                  │                 │
│ 找到下一个      │   返回 done      │  修复错误字段   │
│ 需要操作的字段  │                  │                 │
└────────┬────────┴──────────────────┴────────┬────────┘
         ↓                                     ↓
    执行操作                              执行修复操作
         ↓                                     ↓
    返回截图观察 ←─────────────────────────────┘
```

---

遵循以上规范，确保 Agent 操作的一致性、准确性和可预测性。

