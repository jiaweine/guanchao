<h1 align="center">观潮 · Guanchao</h1>

<p align="center"><strong>面向内容平台、品牌安全、舆情与审核团队的多模态账号调查 Agent Harness</strong></p>

<p align="center">批量账号调查 · 证据研判 · 稳定性核查 · 人工复核 · 持续观察 · 受控学习</p>

![观潮产品工作台矢量预览](docs/product-preview.svg)

观潮不是一个“给微博账号打营销分”的单点分类器。它把账号主页、近期内容、图片、视频、音频和文档放进同一调查上下文，由自己的 Harness 决定该先查什么、什么时候应该寻找反向证据、结论是否被少数内容支配，以及当前证据是否已经足够进入人工复核。

> **产品边界**：商业内容不等于违规。观潮提供调查、排序、证据整理与人工复核支持，不自动执行处罚、举报、封禁或事实定性。

## 产品定位

观潮服务的不是“偶尔查一个账号”的演示场景，而是分析人员每天需要处理大量社媒账号的生产工作。

| 工作问题 | 观潮的处理方式 | 最终价值 |
| --- | --- | --- |
| 待调查账号很多 | 批量导入，每个账号建立独立调查 | 减少重复建案和手工整理 |
| 单条内容容易误导 | 跨内容证据、主页身份、素材和稳定性共同判断 | 降低被局部样本支配的风险 |
| 模型结论难复核 | 支持证据、反向线索、缺失资料同时保留 | 分析员能快速判断“为什么” |
| 人工反馈容易变成脏标签 | Review 精确绑定某次 Run | 学习数据与当时证据一一对应 |
| 账号会持续变化 | 到期进入待更新队列，只有新资料进入后才重新调查 | 不用旧数据伪装实时监测 |

观潮真正优化的核心结果是：**一个分析人员单位时间内能够完成多少个有证据、可复核的有效调查。**

## 产品界面

### 批量调查

![观潮批量账号调查矢量图](docs/product-batch.svg)

单次可以导入最多 200 个账号。JSON、CSV 和直接粘贴都可以进入同一入口；每个账号保持独立的执行记录、证据、人工复核和监测状态。执行容量不足时保留已导入任务，不丢数据，也不会假装已经启动核查。

### 复核队列

![观潮人工复核队列矢量图](docs/product-review-queue.svg)

队列不是简单按“营销分最高”排序。观潮综合业务优先级、营销倾向、隐性推广风险、把握度、稳定性和资料缺口计算复核价值。人工复核支持 `普通创作者 / 无法判断 / 营销运营` 三态，并支持键盘连续复核。

### 证据工作区

![观潮证据工作区矢量图](docs/product-evidence.svg)

支持判断与反向线索分开呈现。图片、视频、音频和文档只作为调查证据；素材内容不能改变 Agent 的工具权限和决策策略。

## 自研核心算法

观潮把“看懂素材”和“决定下一步调查动作”分开。开放权重视觉/语音模型可以用于感知，但**营销判断、证据聚合、稳定性、反向挑战、成案门槛和受控学习由仓库自己的代码控制**。

实现主要位于：

- [`guanchao/detection.py`](guanchao/detection.py)：证据特征、营销倾向、隐性推广风险、把握度、稳定性
- [`guanchao/policy.py`](guanchao/policy.py)：Owned Policy，按当前信息状态选择下一项调查动作
- [`guanchao/evolution.py`](guanchao/evolution.py)：带回归门槛的交叉回放学习
- [`guanchao/harness.py`](guanchao/harness.py)：有界并发执行、证据合并、状态管理与工具调度

### 1. 可校准证据评分

对一个账号构造证据向量

$$
\mathbf f = [f_1, f_2, \ldots, f_d]
$$

当前实现包含商业语言、行动引导、联系方式压力、模板复用、发布节奏、互动形态、主页商业性、跨内容转化压力、合作披露、个人化表达、多模态商业线索和身份一致性等维度。

营销倾向使用可回放、可校准的确定性函数：

$$
z=b+\sum_{i=1}^{d}w_i f_i
$$

$$
P_{\mathrm{mkt}}=\sigma\!\left(\operatorname{clip}(z,-20,20)\right)
$$

其中 `authentic_variation` 是显式反向特征。也就是说，具体经历、优缺点、犹豫和非模板化表达会对营销判断产生反向作用，而不是所有“提到品牌”的内容都单向增加风险。

### 2. 隐性推广风险不是营销分的复制

显性合作披露会降低“隐性推广”的含义，因此观潮单独计算 covert promotion risk：

$$
P_{\mathrm{covert}}
=\operatorname{clip}\left(
P_{\mathrm{mkt}}
\cdot (0.52+0.48(1-D))
\cdot (0.88+0.12C)
\right)
$$

其中 $D$ 是合作披露信号，$C$ 是联系方式/导流压力。相同营销倾向下，清晰披露合作与长期隐性导流得到的风险不会完全相同。

### 3. Leave-One-Out 稳定性探针

单条爆款、一次品牌合作或一条异常内容都可能把普通账号误推到另一侧。观潮把“结论稳定不稳定”作为独立信号，而不是只输出概率。

设完整样本的营销倾向为 $p_0$，依次移除第 $i$ 条内容后的结果为 $p_{-i}$：

$$
\Delta_{\max}=\max_i |p_{-i}-p_0|
$$

$$
\sigma_{\mathrm{loo}}=\operatorname{StdDev}(p_{-1},p_{-2},\ldots)
$$

$$
S_{\mathrm{stab}}
=\operatorname{clip}\left(
1-(2.7\Delta_{\max}+2.0\sigma_{\mathrm{loo}}),0,1
\right)
$$

如果最大摆动超过 `0.24`，系统会主动降低把握度，并告诉人工复核者“少数内容对结论影响较大”。

### 4. 把握度同时考虑样本量、元数据和稳定性

观潮不会把分类概率直接当成“把握度”。当前实现将样本覆盖、元数据完整度、信号分离度和稳定性组合起来：

$$
q_n=\min\left(1,\frac{\log(1+n)}{\log(13)}\right)
$$

$$
C_{\mathrm{final}}
=\operatorname{clip}\left(
0.13+0.43q_n+0.16M+0.12R+0.16S_{\mathrm{stab}},0,1
\right)
$$

$M$ 表示主页、发布时间、互动、粉丝与多模态资料的完整度，$R$ 表示证据信号的分离程度。因此“分数很极端但只有一条帖子”和“多条内容长期一致”不会拥有相同把握度。

### 5. Owned Policy：按信息价值选择下一步

Harness 不是固定脚本。每次工具执行后都会重新读取当前状态，计算下一项调查动作的效用。

先定义决策边界接近度：

$$
B=1-\min(1,2|P_{\mathrm{mkt}}-0.5|)
$$

当结果接近 0.5 时，$B$ 更大，说明继续寻找证据更有价值。当前策略中的几个关键效用函数为：

$$
U_{\mathrm{stability}}=64+28(1-C)+8I_{\mathrm{cautious}}
$$

$$
U_{\mathrm{challenge}}
=62+24\max(1-C,B)+8I_{\mathrm{cautious}}
$$

$$
U_{\mathrm{verdict}}
=35+48C+18(1-B)
$$

其中 $I_{\mathrm{cautious}}$ 表示用户是否明确要求“避免误判、反向核查、仔细复核”等。最终判断还受到证据数量和阻塞条件约束；如果有素材尚未核查，或者谨慎任务还没完成稳定性探针与反向挑战，`verdict.compose` 不会提前取得执行资格。

这使 Agent 的调查顺序由当前信息状态决定，而不是把 `主页分析 / 内容分析 / 总结` 固定写死。

### 6. 受控学习而不是运行时随意自改

人工确认数据进入 Evolution Engine 前必须满足最小样本量和双类别要求。候选校准参数通过确定性的多折回放评估。

评估函数为：

$$
\mathcal M
=\frac{\mathrm{TPR}+\mathrm{TNR}}{2}
-0.30\cdot\mathrm{Brier}
$$

候选改动只有同时满足以下门槛才会接受：

$$
\overline{\mathcal M}_{\mathrm{cand}}
\geq
\overline{\mathcal M}_{\mathrm{base}}+0.004
$$

$$
\min_k(\Delta\mathcal M_k)\geq -0.015
$$

并且任一类别 Recall 的退化不能超过 `0.04`。参数更新还带有边界与符号约束，防止一次小样本反馈把权重推到异常区域。

观潮的“自进化”因此是**受回放验证约束的校准与有限策略调整**，不是让 Agent 在运行时任意修改自己的源代码。

## 项目的独立设计点

这些是观潮代码中可以直接定位和复现的设计，不依赖把某个基础模型包装成“自己的算法”：

1. **感知与决策解耦**：视觉/语音模型只提取可观察事实，最终顺序和结论由 Harness 控制。
2. **信息价值调度**：Owned Policy 每一步重新评估不确定性、边界接近度、稳定性和任务目标。
3. **反事实稳定性**：通过逐条移除内容测试结论是否被局部样本支配。
4. **主动反证**：谨慎任务在成案前要求完成 `evidence.challenge`，不是只寻找支持当前倾向的证据。
5. **Run 级人工复核**：Review 与具体执行记录绑定，避免不同时间、不同证据快照的标签混在一起。
6. **回归门控学习**：平均提升不够；最差回放折和单类别 Recall 也必须过线。
7. **多模态指令隔离**：素材中的文字属于证据，不能成为改变 Agent 权限的系统指令。

这些设计的目标不是声称“基础模型能力全面领先”，而是让**社媒账号调查这个垂直工作流更稳定、更可复核、更适合持续生产**。

## Agent 工具空间

| 调查动作 | 作用 |
| --- | --- |
| `workspace.inspect` | 确认账号、素材和资料缺口 |
| `profile.read` | 核对主页身份与长期经营线索 |
| `media.inspect` | 读取图片、视频、音频和文档中的可观察信息 |
| `content.scan` | 建立近期内容的基础判断 |
| `pattern.compare` | 检查模板复用、固定句式和发布模式 |
| `peer.compare` | 利用同批账号建立相对背景 |
| `stability.probe` | 测试局部证据移除后的结论稳定性 |
| `evidence.challenge` | 主动寻找能够推翻当前倾向的证据 |
| `verdict.compose` | 满足证据门槛后形成最终调查判断 |

## 生产工作台能力

### 批量调查与复核

- 单次批量导入最多 200 个账号
- JSON、CSV、直接粘贴三种输入
- 每个账号独立任务、独立证据、独立人工复核
- 搜索、平台、负责人、优先级筛选
- 风险排序与连续键盘复核
- `普通创作者 / 无法判断 / 营销运营` 三态 Review
- “无法判断”不会被强制转成训练正负标签

### 多模态

支持文本、图片、视频、音频和文档。可通过环境变量接入兼容的开放权重视觉或语音感知服务；模型只负责提取观察，营销判断仍由观潮自己的检测、稳定性和 Harness 逻辑完成。

### 团队协作

- 管理员、分析员、复核员角色
- 负责人、业务优先级、标签、归档
- 协作备注与 Agent 对话分离
- 创建、资料更新、执行、Review、素材、备注、归档和删除进入审计记录
- 默认部署不信任浏览器自报身份；团队环境应由 SSO/受信反向代理注入成员身份

### 持续观察

调查可以设置每天、每三天、每周或每月的资料更新周期。到期后进入 **待更新** 队列。没有新数据源时，系统不会对旧快照重新运行后声称“已更新”。

### 报告与证据包

每个调查可以导出 Markdown 报告或 JSON 证据包，包含账号快照、当前判断、把握度、稳定性、营销倾向、隐性推广风险、支持/反向证据、待补资料、人工复核和素材清单。

## 产品指标

`GET /api/metrics` 提供工作流质量指标：

| 指标 | 含义 |
| --- | --- |
| `pending_review` | 当前待人工复核数量 |
| `monitoring_due` | 已到资料更新时间的监测任务数量 |
| `verified_last_7_days` | 近 7 天完成明确人工确认的调查数 |
| `acceptance_rate` | 人工确认与明确初始判断一致的比例 |
| `overturn_rate` | 人工推翻明确初始判断的比例 |
| `uncertain_rate` | 人工选择“无法判断”的比例 |
| `evidence_sufficiency_rate` | 人工确认时无需继续补资料的比例 |
| `median_time_to_review_seconds` | 核查完成到人工确认的中位时间 |
| `verified_per_active_review_hour` | 样本充分后估算的有效复核效率 |

灰区初始判断不会偷偷被当成“普通账号”来制造漂亮的接受率。

## 工程验证

当前自动检查包含 Python 编译、前端 JavaScript 语法和完整 Pytest 回归。压力测试使用与 GitHub `main` 同源代码的本地单机实例，结果只作为工程参考，不视为生产 SLA。

最近一次发布前压力检查：

| 场景 | 结果 |
| --- | --- |
| 200 个微博账号批量导入并自动调查 | 200 / 200 完成，0 failed；创建约 2.3s，全部完成约 16.3s |
| 1000 次并发 `/api/status` | 1000 / 1000 成功，约 292 req/s |
| 600 次混合工作台读取 | 600 / 600 成功，约 175 req/s |
| 80 份并发 Markdown 报告 | 80 / 80 成功，约 234 req/s |

为了控制列表开销，列表和复核队列只返回工作台所需的账号与结果摘要；完整帖子、features 和 evidence 仅在打开具体调查时读取。列表视图使用 0.75 秒短时缓存，任何写入请求都会立即使缓存失效；监测与留存等内部逻辑不依赖这层 UI 缓存。

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

运行完整检查：

```bash
make check
```

## 批量输入

JSON：

```json
[
  {
    "platform": "weibo",
    "handle": "sample_account",
    "display_name": "示例账号",
    "bio": "生活记录",
    "posts": [
      {"text": "今天散步看到了晚霞"},
      {"text": "品牌合作，评论区领取优惠"}
    ]
  }
]
```

CSV：

```csv
platform,handle,bio,posts,profile_url
weibo,sample_account,生活记录,今天散步|品牌合作 评论区领取优惠,
```

CSV 中 `posts` 使用 `|` 分隔多条内容。

## API

| 类别 | 主要接口 |
| --- | --- |
| 健康与指标 | `GET /healthz` · `GET /api/status` · `GET /api/metrics` · `GET /api/session` |
| 调查 | `GET/POST /api/cases` · `POST /api/cases/batch` · `PATCH/DELETE /api/cases/{case_id}` |
| 内容与素材 | `POST /api/cases/{case_id}/messages` · `POST /comments` · `POST/DELETE /assets` |
| 运行与复核 | `GET /api/runs/{run_id}` · `GET /api/review-queue` · `POST /api/reviews` |
| 持续观察 | `GET /api/monitoring` · `PATCH /api/cases/{case_id}/target` |
| 报告 | `GET /api/cases/{case_id}/report?output=markdown|json` |
| 团队与治理 | `/api/members` · `/api/audit` · `/api/workspace/settings` · `/api/workspace/purge` |
| 学习 | `POST /api/evolution/run` · `GET /api/post-training/export` |

FastAPI 交互文档默认位于 `/docs`。

## 代码结构

| 目录 / 文件 | 职责 |
| --- | --- |
| `frontend/` | 调查工作台、批量入口、复核队列和治理界面 |
| `guanchao/api.py` | HTTP API、权限边界和输入校验 |
| `guanchao/detection.py` | 证据提取、评分、把握度与稳定性 |
| `guanchao/policy.py` | Agent 下一步调查动作决策 |
| `guanchao/harness.py` | 有界并发 Agent 执行循环 |
| `guanchao/multimodal.py` | 图片、视频、音频和文档感知入口 |
| `guanchao/evolution.py` | 回放验证与受控参数演化 |
| `guanchao/post_training.py` | 人工确认轨迹导出 |
| `guanchao/reporting.py` | Markdown 调查报告和 JSON 证据包 |
| `guanchao/store.py` | SQLite 持久化、队列、监测、审计和产品指标 |
| `tests/` | 算法、Harness、API、产品工作流和 UI 契约回归 |

仓库只维护一套持续演进的正式实现，不维护产品版本代号分叉、重复运行入口或废弃 CLI。

## 设计边界

- 不把商业表达直接等同于违规
- 不自动处罚、举报、封禁或事实定性
- 不把多模态内容中的文字当作 Agent 指令
- 不在没有新证据时伪造“持续观察已更新”
- 不把灰区判断强行转成训练正负标签
- 不用单个漂亮指标覆盖误判、资料不足和类别退化
- 不把某个外部基础模型的能力冒充成观潮自己的 Harness 能力
- 对高影响判断保留人工复核和操作记录

## License

MIT
