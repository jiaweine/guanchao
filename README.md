# 观潮 · Guanchao

**面向内容平台、品牌安全、舆情与审核团队的多模态账号调查工作台。**

观潮不是“给账号打一个营销分”的分类器。它把账号主页、近期内容、图片、视频、音频和文档放进同一条调查任务，由 Agent 自己决定先查什么、什么时候需要反向核对、结论是否稳定、还缺什么证据；完成后进入人工复核队列，复核结果继续进入受控学习闭环。

真正要优化的不是单次模型分数，而是：**一个分析人员单位时间内能够完成多少个有证据、可复核的有效调查。**

![观潮产品界面矢量预览](docs/product-preview.svg)

> 商业内容不等于违规。观潮只辅助调查、排序和复核，不自动执行处罚、举报、封禁或事实认定。

## 核心工作流

```text
单个账号 / 批量账号 / 多模态素材
              │
              ▼
        创建独立调查任务
              │
              ▼
      Agent 自主选择核查动作
      ├─ 主页与内容
      ├─ 多模态素材
      ├─ 重复模式与同批对照
      ├─ 稳定性检查
      └─ 反向证据挑战
              │
              ▼
       形成证据支持的判断
              │
              ▼
       按复核价值进入队列
              │
              ▼
普通创作者 / 无法判断 / 营销运营
              │
              ▼
  复核记录 → 回放评估 → 受控学习
```

对于一批账号，观潮会为每个账号建立独立调查，后台并发核查，完成后综合营销倾向、隐性推广风险、把握度、稳定性、资料缺口和业务优先级生成复核顺序。人工确认一条后，工作台会自动进入下一条待复核任务。

## 产品能力

### 批量调查与高效复核

- 单次批量导入最多 200 个账号。
- 支持 JSON、CSV 或直接粘贴账号列表。
- 每个账号拥有独立任务、执行记录、证据和人工复核。
- 待复核队列默认按“最值得先看”排序，也可以按营销倾向或最近完成排序。
- 支持搜索、平台、负责人、业务优先级筛选。
- 人工复核支持 `普通创作者 / 无法判断 / 营销运营` 三态；“无法判断”不会被硬塞进二元训练数据。
- 键盘 `1 / 2 / 3` 可以连续复核，提交后自动进入下一条。

### 多模态调查

文本、图片、视频、音频和文档都属于调查证据，而不是 Agent 指令。素材中即使包含“忽略之前要求”等文本，也不能改变工具选择和执行权限。

本地感知层可以通过环境变量接入兼容的开放权重视觉或语音服务；感知层只负责提取可观察事实，最终调查顺序、反向核查、稳定性判断和成案仍由观潮自己的 Harness 控制。

### 持续监测

调查可以加入监测，并设置每天、每三天、每周或每月的资料更新周期。

观潮不会在没有真实数据源的情况下假装“自动抓到了新内容”。到期后任务进入 **待更新** 队列；分析员补充最新主页、内容或素材后，系统更新资料时间并可立即重新调查。这样能够保证“最新”确实来自新的证据，而不是对旧快照重复运行。

### 团队协作与治理

- 工作空间成员角色：管理员、分析员、复核员。
- 调查可以指定负责人、业务优先级和标签。
- 关键操作进入审计记录：创建、资料更新、运行、人工复核、素材变化、协作备注、归档、删除等。
- 协作备注与 Agent 对话分离：给同事留下的上下文不会误触发一次新核查，并保留作者与时间。
- 已归档任务可以恢复或永久删除。
- 可以设置归档数据留存天数；自动留存策略只清理到期的已归档调查，不碰仍在工作的任务。

浏览器不能自行选择工作空间身份。默认部署只使用本地管理员身份，并忽略客户端发送的身份头。团队部署应在观潮前接企业 SSO、反向代理认证或零信任网关，由网关**删除客户端原始身份头后重新注入**受信的 `X-Guanchao-Actor`，再设置 `GUANCHAO_TRUST_ACTOR_HEADER=1`。仓库本身不假装提供一个没有认证基础的“安全登录页”。

### 调查报告与分享

每个调查可以导出 Markdown 报告或 JSON 证据包，包含：

- 调查目标与账号快照
- 当前判断
- 把握度、稳定性、营销倾向与隐性推广风险
- 支持证据、反向线索和待补资料
- 当前人工复核结果
- 素材清单

工作台可以复制带 `case` 参数的调查链接，用于同一部署环境中的团队协作。

## Agent Harness

观潮的 Agent 控制层不是固定脚本。每一步都会根据当前不确定性、已有证据、资料完整度、稳定性、反向证据需求和任务目标重新决定下一项调查动作。

主要动作包括：

- `workspace.inspect`：整理现有账号和素材范围
- `profile.read`：核对主页信息
- `media.inspect`：读取多模态素材产生的观察
- `content.scan`：检查近期内容
- `pattern.compare`：对照重复表达、节奏和转化模式
- `peer.compare`：同批账号之间进行背景比较
- `stability.probe`：移除局部证据后重新判断，避免被单条内容支配
- `evidence.challenge`：主动寻找能推翻当前倾向的证据
- `verdict.compose`：只有满足完成条件后才形成最终判断

开放权重模型适合放在“看 / 听 / 读”的感知层；Agent 决策、证据门槛、稳定性探针、反向挑战和受控学习仍由观潮自己的代码实现。

## 人工复核与学习

人工复核精确绑定某一次 `run_id`，而不是模糊地绑定整个 Case。同一次执行的复核再次提交时会更新原记录，不会产生相互冲突的重复标签。

学习流程采用交叉回放和回归门槛：

- 样本不足时拒绝更新；
- 两类人工确认都必须存在；
- 候选改动需要在多个回放折上稳定提升；
- 单类结果明显退化时拒绝接受；
- “无法判断”不作为正负标签；
- 只有通过门槛的校准与有限策略参数才会保存。

`GET /api/post-training/export` 可以导出与 Harness 轨迹绑定的 JSONL，用于后续 SFT、偏好学习或策略研究，而运行时不依赖某个外部训练框架。

## 产品指标

`GET /api/metrics` 返回工作流质量指标，而不只是工程健康状态：

- `pending_review`：待人工复核数量
- `monitoring_due`：需要更新资料的监测任务数量
- `verified_last_7_days`：近 7 天完成明确人工确认的调查数
- `acceptance_rate`：人工确认与明确的初始判断一致的比例
- `overturn_rate`：人工推翻明确初始判断的比例
- `uncertain_rate`：人工选择“无法判断”的比例
- `evidence_sufficiency_rate`：人工确认时无需补资料的比例
- `median_time_to_review_seconds`：核查完成到人工确认的中位时间
- `verified_per_active_review_hour`：有足够有效复核样本后估算的活跃复核效率

最后一项只在至少积累 3 次带打开记录的明确复核后计算，避免用极少样本制造漂亮但无意义的数字。

## 快速开始

```bash
git clone https://github.com/jiaweine/guanchao.git
cd guanchao
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
make run
```

打开 `http://127.0.0.1:8765`。

运行检查：

```bash
make check
```

## 批量输入

### JSON

```json
[
  {
    "platform": "weibo",
    "handle": "sample_account",
    "display_name": "示例账号",
    "bio": "生活记录",
    "profile_url": "https://example.invalid/profile",
    "posts": [
      {"text": "今天散步看到了晚霞"},
      {"text": "品牌合作，评论区领取优惠"}
    ]
  }
]
```

### CSV

```csv
platform,handle,bio,posts,profile_url
weibo,sample_account,生活记录,今天散步|品牌合作 评论区领取优惠,
```

CSV 中 `posts` 使用 `|` 分隔多条内容。

## 主要 API

```text
GET    /healthz
GET    /api/status
GET    /api/metrics
GET    /api/session

GET    /api/cases
POST   /api/cases
POST   /api/cases/batch
GET    /api/cases/{case_id}
PATCH  /api/cases/{case_id}
DELETE /api/cases/{case_id}
PATCH  /api/cases/{case_id}/target

POST   /api/cases/{case_id}/messages
POST   /api/cases/{case_id}/comments
POST   /api/cases/{case_id}/assets
DELETE /api/cases/{case_id}/assets/{asset_id}
GET    /api/runs/{run_id}

GET    /api/review-queue
POST   /api/reviews
GET    /api/monitoring

GET    /api/cases/{case_id}/report?output=markdown
GET    /api/cases/{case_id}/report?output=json

GET    /api/members
POST   /api/members
DELETE /api/members/{member_id}
GET    /api/audit
POST   /api/events

GET    /api/workspace/settings
PUT    /api/workspace/settings
POST   /api/workspace/purge

POST   /api/evolution/run
GET    /api/post-training/export
```

## 工程结构

```text
.
├── frontend/
│   ├── index.html
│   ├── app.css
│   └── app.js
├── guanchao/
│   ├── api.py
│   ├── detection.py
│   ├── domain.py
│   ├── evolution.py
│   ├── harness.py
│   ├── multimodal.py
│   ├── policy.py
│   ├── post_training.py
│   ├── reporting.py
│   ├── sample_data.py
│   ├── store.py
│   ├── tools.py
│   └── verifier.py
├── tests/
├── Dockerfile
├── Makefile
└── requirements.txt
```

仓库只保留一套持续演进的实现；不维护产品代号分支、重复运行入口或废弃 CLI。

## 设计边界

- 不把商业表达直接等同于违规。
- 不自动处罚、举报、封禁或事实定性。
- 不把多模态内容中的文字当作 Agent 指令。
- 不在没有新证据时伪造“持续监测已更新”。
- 不用单个漂亮指标覆盖误判、无法判断和资料不足。
- 不把某个外部大模型的能力冒充成观潮自己的 Harness 能力。
- 对高影响决策保留人工复核和完整操作记录。

## License

MIT
