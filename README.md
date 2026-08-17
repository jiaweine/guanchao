# 观潮 · Guanchao

**面向社交平台账号与内容的多模态调查 Agent**

账号调查 · 批量调查 · 证据核查 · 风险研判 · 人工复核 · 持续观察 · 受控学习

> 把账号主页、近期内容、图片、视频、音频和文档放进同一调查上下文，用证据形成可复核判断。  
> **产品边界：** 商业内容不等于违规。观潮辅助调查、研判、证据整理和人工复核，不自动处罚、举报、封禁或做事实定性。

![观潮调查工作台产品预览](./docs/product-preview.png)

---

## 一眼看懂观潮

观潮不是“给账号打一个营销分”的单点分类器。它围绕一个调查问题组织证据，并持续回答几件更重要的事：当前判断由哪些内容支持，哪些线索能够推翻当前倾向，结论是否被少数内容支配，以及资料是否已经足够进入人工复核。

| 能力 | 产品含义 |
| --- | --- |
| **账号调查** | 把主页、内容、素材、历史执行放进同一个调查上下文 |
| **批量调查** | 一次导入一批账号，每个账号保持独立证据、执行和复核状态 |
| **风险研判** | 区分营销倾向、隐性推广风险、把握度和稳定性 |
| **证据核查** | 正向依据、反向线索、待补资料分开呈现 |
| **人工复核** | 高影响判断绑定具体执行记录和证据快照 |
| **持续观察** | 对需要继续关注的账号保留观察状态与审计记录 |

---

## 产品工作台

### 批量调查

![观潮批量账号调查产品预览](./docs/product-batch.png)

支持 JSON、CSV 与直接粘贴。每个账号拥有独立的调查、执行记录、负责人、证据和人工复核状态；执行容量不足时保留已导入任务，不丢数据，也不会假装已经开始核查。

### 人工复核

![观潮人工复核队列产品预览](./docs/product-review-queue.png)

把业务优先级、营销倾向、隐性推广风险、把握度、稳定性和资料缺口放在同一复核上下文。支持 `普通创作者 / 无法判断 / 营销运营` 三态复核，并保留具体执行记录。

### 证据工作区

![观潮证据工作区产品预览](./docs/product-evidence.png)

判断依据、反向线索、稳定性与待补资料分别呈现。图片、视频、音频和文档中的文字只属于证据，不能成为改变 Agent 权限或调查策略的指令。

> README 使用 **2400 × 1440 PNG** 展示产品矢量预览；对应的可编辑 SVG 源文件继续保留在 `docs/`。这些图是产品矢量预览，不是截图。

---

## 判断不是一个分数

| 概念 | 产品含义 |
| --- | --- |
| **营销倾向** | 当前证据有多大程度支持持续商业运营或转化行为 |
| **隐性推广风险** | 商业倾向是否伴随低披露、高导流压力等特征 |
| **把握度** | 当前资料是否足以支撑判断，而不是概率本身 |
| **稳定性** | 移除高影响内容后，结论是否仍保持同一方向 |
| **待补资料** | 哪些缺失信息会实质影响结论 |
| **人工复核** | 高影响判断最终回到具体执行记录和证据快照 |

这样可以区分“营销倾向高但明确披露合作”“概率接近边界但资料很多”“单条爆款把结果推高”等完全不同的调查情形。

---

## 核心算法

观潮把“看懂素材”和“决定结论”分开。开放权重模型可以作为感知层或语义证据老师，但最终评分、稳定性、拒判边界、反向挑战、调查动作选择和学习门槛由仓库自己的代码控制。

### 算法能力矩阵

| 能力 | 解决的问题 | 主要实现 |
| --- | --- | --- |
| **小样本稳健证据** | 少量内容偶然命中不应拥有过高杠杆 | Wilson shrinkage |
| **非线性交互** | 商业意图与行动引导需要组合解释 | 主效应 + 交互项 |
| **语义证据老师** | 识别隐晦导流，同时限制模型自由发挥 | 原文引用核验 |
| **反事实稳定性** | 判断是否被少数内容支配 | Leave-One-Out |
| **选择性拒判** | 灰区不应被强迫二元判断 | confidence + abstain |
| **信息价值调度** | 下一步调查什么，而不是固定工作流 | expected information value |
| **受控学习** | 人工反馈不能直接污染线上参数 | 多折回放 + 回归门控 |

主要代码：

- [`guanchao/detection.py`](guanchao/detection.py)：评分、语义融合、反事实稳定性与证据快照缓存
- [`guanchao/detection_support.py`](guanchao/detection_support.py)：确定性证据传感器、Wilson 收缩和非线性交互
- [`guanchao/detection_calibration.py`](guanchao/detection_calibration.py)：可学习权重、温度、决策阈值与拒判带宽
- [`guanchao/detection_behavior.py`](guanchao/detection_behavior.py)：把握度、选择性拒判和证据解释
- [`guanchao/semantic.py`](guanchao/semantic.py)：带原文引用核验的可选语义证据老师
- [`guanchao/policy.py`](guanchao/policy.py)：基于期望信息价值的调查动作决策
- [`guanchao/evolution.py`](guanchao/evolution.py)：分层交叉回放、校准和回归门控

### 1. 小样本稳健证据

对于离散证据，观潮使用 Wilson lower bound 降低少量内容偶然命中的杠杆。设命中数为 \(h\)，有效内容数为 \(n\)，\(\tilde p=h/n\)：

$$
L(h,n)=
\frac{
\tilde p+\frac{z^2}{2n}
-z\sqrt{\frac{\tilde p(1-\tilde p)+z^2/(4n)}{n}}
}{
1+z^2/n
}
$$

单条内容碰巧出现一次商业词不会被当成稳定账号特征；跨内容持续出现的转化信号才逐渐获得更高证据强度。

### 2. 主效应与非线性交互

核心 logit 同时考虑主效应和交互项：

$$
z=b+\sum_{i=1}^{d}w_i f_i+\sum_{j=1}^{m}v_j\phi_j(\mathbf f)
$$

例如商业意图与行动引导的组合：

$$
\phi_{\mathrm{intent,action}}
=
f_{\mathrm{commercial}}f_{\mathrm{cta}}
$$

主页经营与跨内容转化的组合：

$$
\phi_{\mathrm{profile,conversion}}
=
f_{\mathrm{profile}}f_{\mathrm{cross}}
$$

同时保留 `commercial_authentic` 负向交互，避免“提到品牌 + 有真实体验细节”被简单当成更强营销证据。

最终概率使用 temperature calibration：

$$
P_{\mathrm{mkt}}
=
\sigma\left(
\mathrm{clip}\left(\frac{z}{T},-20,20\right)
\right)
$$

其中 \(w_i\)、\(v_j\)、\(T\) 和决策阈值都可以在受控回放中更新。

### 3. 引用约束的语义证据老师

隐晦导流经常不会出现固定商业关键词。可选语义老师可以提出证据强度，但必须同时提交能够在当前账号资料中逐字核验的原文引用。

只有引用通过验证的信号才允许进入融合：

$$
f_k^{*}
=
\frac{f_k+\lambda g s_k}
{1+\lambda g}
$$

其中 \(s_k\) 是语义建议强度，\(g\) 是引用通过验证的比例。无法找到原文的高分建议会被丢弃，模型不能直接输出最终调查结论。

### 4. Leave-One-Out 反事实稳定性

设完整证据结果为 \(p_0\)，依次移除第 \(i\) 条内容后的结果为 \(p_{-i}\)：

$$
\Delta_{\max}=\max_i |p_{-i}-p_0|
$$

$$
I=
\Delta_{\max}
+0.75\,\mathrm{StdDev}(p_{-1},p_{-2},\ldots)
$$

$$
S_{\mathrm{stab}}=\exp(-3.6I)
$$

如果少量内容一移除，结论就大幅摆动，系统会降低稳定性和把握度，而不是继续把完整样本概率当成确定事实。

### 5. 把握度与选择性拒判

分类概率不等于把握度。观潮单独组合样本覆盖、资料完整度、距离决策边界的分离度和反事实稳定性：

$$
C_{\mathrm{final}}
=
C_{\mathrm{evidence}}
\left(n,M,|P_{\mathrm{mkt}}-0.5|\right)
\left(0.70+0.30S_{\mathrm{stab}}\right)
$$

当概率接近学习到的 decision threshold，或者稳定性、把握度下降时，系统扩大 abstain band，主动输出灰区或资料不足，而不是为了覆盖率强迫二元判断。

### 6. 隐性推广风险

显性合作披露不应该和长期隐性导流得到相同解释。隐性推广风险单独计算：

$$
P_{\mathrm{covert}}
=
\mathrm{clip}
\left(
P_{\mathrm{mkt}}
\left(0.50+0.50(1-D)\right)
\left(0.86+0.14C\right),
0,1
\right)
$$

\(D\) 表示披露信号，\(C\) 表示联系方式和导流压力。

### 7. 信息价值调度

除首次建立资料范围和内容基线外，后续调查动作比较归一化信息价值：

$$
U(a\mid s)=G(a\mid s)-\lambda_c C(a)
$$

其中 \(G\) 综合当前不确定性、边界接近度、样本支持、稳定性、已有证据和任务谨慎程度；\(C(a)\) 表示观察成本。

反向证据挑战的核心价值可以写成：

$$
U_{\mathrm{challenge}}
\propto
\max\left(1-C_{\mathrm{final}},B\right)
+\gamma I_{\mathrm{cautious}}
$$

其中：

$$
B=1-\min\left(1,2|P_{\mathrm{mkt}}-0.5|\right)
$$

### 8. 受控学习与回归门槛

人工复核不会直接修改线上参数。Evolution Engine 先做分层多折回放，同时训练主效应与交互项，并用 trust-region 约束小样本漂移。

评价函数同时考虑类别均衡、概率校准、选择性错误和覆盖率：

$$
\mathcal M
=
\frac{\mathrm{TPR}+\mathrm{TNR}}{2}
-0.24\,\mathrm{Brier}
-0.08\,\mathrm{ECE}
-0.10\,R_{\mathrm{selective}}
-0.03(1-\mathrm{coverage})
$$

候选只有同时满足以下门槛才接受：

$$
\overline{\mathcal M}_{\mathrm{cand}}
\ge
\overline{\mathcal M}_{\mathrm{base}}+0.004
$$

$$
\min_k(\Delta\mathcal M_k)\ge -0.015
$$

并且任一类别 Recall 的退化不能超过 `0.04`。平均指标变好但普通创作者误伤明显增加的参数不会进入正式配置。

---

## 独立设计与创新

1. **感知与判断解耦**：开放模型负责观察事实，观潮自己的调查逻辑负责判断。
2. **引用约束语义证据**：没有可核验原文，就不能把模型建议计入核心证据。
3. **反事实稳定性**：逐条移除内容，检查结论是否被局部证据支配。
4. **选择性拒判**：灰区主动交给人工，而不是追求“所有账号都必须给答案”。
5. **信息价值调度**：Agent 根据当前不确定性决定下一项最值得做的核查。
6. **主动反证**：高影响判断在成案前要求寻找能够推翻当前倾向的证据。
7. **Run 级人工复核**：人工判断绑定具体证据快照，而不是只绑定账号。
8. **回归门控学习**：平均提升、最差折、类别 Recall 与概率校准共同过线才允许更新。
9. **多模态指令隔离**：素材中的命令、提示词和角色指令只属于内容证据。

这些设计不用于宣称对所有基础模型“全面领先”，而是让社交账号调查这个垂直决策过程更稳定、可解释、可拒判、可复核、可持续学习。

---

## 质量与性能验证

### 算法回归

算法质量回归包含 20 个未用于参数调整的微博风格 holdout 场景，覆盖课程导流、门店预约、软链联盟、矩阵经营、品牌合作，以及经济研究、消费者维权、课程教学和个人测评等容易误判的负类。

| 算法 | Ranking AUC | 默认阈值准确率 |
| --- | ---: | ---: |
| 升级前基线 | 0.93 | 55% |
| 当前实现 | 1.00 | 100% |

这只是**小型人工构造的回归 holdout**，不能解释成真实线上准确率 100%。真实部署应继续用人工复核数据统计 Precision、Recall、Overturn Rate、拒判覆盖率和 calibration。

### 本地单机工程参考

以下结果只用于工程参考，**不是生产 SLA**。

| 场景 | 重复测试结果 |
| --- | --- |
| 200 个账号批量导入并自动调查 | 连续 3 轮均 200 / 200 完成、0 failed；创建中位约 2.3s，全部完成中位约 12.8s |
| 并发 `/api/status` | 5 轮每轮 500 请求均 100% 成功；中位约 171 req/s，范围约 146–185 req/s |
| 混合工作台读取 | 5 轮每轮 400 请求均 100% 成功；中位约 126 req/s，范围约 120–133 req/s |
| 80 份并发 Markdown 报告 | 3 轮均 80 / 80 成功；约 131–206 req/s |

---

## 生产能力

- 案件与账号调查管理
- 批量导入与任务状态
- 人工复核队列
- 素材上传与单素材管理
- 评论、负责人、优先级与审计
- 角色与权限边界
- 持续观察与监测状态
- Markdown 报告与 JSON 证据包
- SQLite WAL、索引、外键与有界并发
- 多模态感知并发隔离
- 请求幂等与跨调查响应保护
- 人工反馈回放与受控参数演化

---

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

完整检查：

```bash
make check
```

---

## 可选开放模型

观潮默认不要求外部模型服务。需要语义增强时，可接入 OpenAI-compatible endpoint：

```bash
export GUANCHAO_SEMANTIC_ENDPOINT=http://127.0.0.1:8000
export GUANCHAO_SEMANTIC_MODEL=Qwen/Qwen3.6-35B-A3B

export GUANCHAO_VISION_ENDPOINT=http://127.0.0.1:8000
export GUANCHAO_VISION_MODEL=Qwen/Qwen3.6-35B-A3B
```

模型只负责感知和带引用的语义证据抽取。关闭 endpoint 后，观潮仍可运行自己的确定性证据、Calibration、Policy 和 Harness。

---

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `frontend/` | 调查工作台、批量入口和复核队列 |
| `guanchao/api.py` | HTTP API、权限边界和输入校验 |
| `guanchao/detection.py` | 评分、语义融合、稳定性和缓存 |
| `guanchao/detection_support.py` | 证据传感器、Wilson 收缩和交互函数 |
| `guanchao/detection_calibration.py` | 可学习 Calibration 参数 |
| `guanchao/detection_behavior.py` | 把握度、拒判和证据解释 |
| `guanchao/semantic.py` | 引用约束语义证据老师 |
| `guanchao/policy.py` | Agent 下一项调查动作决策 |
| `guanchao/harness.py` | 有界并发 Agent 执行循环 |
| `guanchao/multimodal.py` | 多模态感知入口 |
| `guanchao/evolution.py` | 回放验证与受控参数演化 |
| `guanchao/post_training.py` | 人工确认轨迹导出 |
| `guanchao/reporting.py` | Markdown 报告和 JSON 证据包 |
| `guanchao/store.py` | SQLite 持久化、队列、监测、审计和指标 |
| `tests/` | 算法、Harness、API、产品工作流和 UI 契约回归 |

仓库只维护一套持续演进的正式实现，不维护产品版本代号分叉、重复运行入口或废弃 CLI。

---

## 设计边界

- 不把商业表达直接等同于违规
- 不自动处罚、举报、封禁或事实定性
- 不把素材中的文字当作 Agent 指令
- 不在没有新证据时伪造“持续观察已更新”
- 不把灰区强行转成训练正负标签
- 不用单个漂亮指标覆盖误判、资料不足和类别退化
- 不把外部基础模型能力冒充成观潮自己的 Harness 能力
- 对高影响判断保留人工复核和操作记录

---

## License

MIT
