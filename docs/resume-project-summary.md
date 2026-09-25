# 简历项目素材：Hermes 多模型自适应路由与 Agent 可靠性系统

> 说明：本文所有表述均以项目当前实际完成度为准。系统当前处于 **Production Shadow 已验证 / validated in shadow mode** 阶段：路由决策被真实采集并记录，实际执行的模型仍由网关自身配置决定，自动切换尚未启用。文中不含任何成本、性能或规模量化结论。

## 项目名称建议

- 中文名：**Hermes 多模型自适应路由与 Agent 可靠性系统**
- 英文名：**Privacy-Preserving Adaptive LLM Routing for an Agent Runtime**

## 技术栈

Python · Hermes Agent v0.21.5（Plugin / Hook 架构）· TypeSafe JEV（决策服务）· DeepSeek API · Xiaomi MiMo API · Docker · LLM Tool Calling · Shadow Deployment · Observability · Fault Tolerance · Privacy Engineering

## 项目介绍（2–3 行）

基于 Hermes Agent v0.21.5 的隐私保护自适应 LLM 路由层（Shadow Router），通过单一 `pre_api_request` 插件钩子接入 TypeSafe JEV 决策服务，在快速低成本模型（DeepSeek V4.1 Flash）与高能力模型（MiMo V2.6 Pro）之间给出逐轮路由决策。系统以 Shadow 模式在真实消息网关流量上验证：决策被完整采集与统计，实际执行模型保持不变。设计上以数据最小化、fail-open 与可逆的运行时 kill switch 为约束，在零核心改动的条件下完成集成。

## 项目职责 / 亮点（4–6 条 bullet）

- **多模型路由架构设计**：以单个 `pre_api_request` 观察者钩子接入 Agent 运行时，无 fork、无核心补丁、无代理层；每轮仅在首次 LLM 调用时触发一次路由，重试与后续 tool-loop 迭代显式跳过；决策服务严格为咨询性质，钩子始终返回 `None`，不注入上下文、不修改请求内容。
- **Production Shadow 发布机制**：把"决策质量"与"执行是否安全"两个问题拆开——先在真实消息网关上只采集决策，执行路径完全不动；记录中同时保存 `actual_model`（真实执行模型）与 `would_execute`（未来自动模式会选择的模型），使 Shadow 不变量可被逐条核对；路由可用性不再是代码里的硬编码常量（`MIMO_AVAILABLE` 已不存在），而来自显式配置 `JEV_AVAILABLE_ROUTES`（如 `deepseek_flash,mimo_pro`），其公开默认为空，`router.config.available_routes()` 忽略未知名称——因此未做任何配置时记录里的 `would_execute` 为 `null` 而不是指名某个 provider；一条路由只有在操作者按验证结果显式配置之后才算可执行；`JEV_AVAILABLE_ROUTES` 让部署可用性显式化，并为未来路由准备了配置面，真正新增一条路由仍需要为其扩展 criteria / telemetry / simulation / tests。
- **隐私脱敏与 Routing Dossier**：确定性本地正则脱敏（私钥块、带前缀的 API Key、Bearer Token、`key=value` 密钥、邮箱、IPv4/IPv6、`user@host`、长随机串），不依赖模型与网络；Dossier 只包含当前轮脱敏后截断的文本、布尔任务/风险标记与可用路由列表，不包含历史、长期记忆、工具输出、系统提示词与凭证；检出私钥类内容时整轮不发出，改记本地 Local Privacy Fallback（隐私 fail-closed，可用性 fail-open）；telemetry 为 content-free，字段白名单由测试强制。
- **Fail-open 与运行时 Kill Switch**：超时、HTTP 401/403/429/5xx、`urlerror_*`、畸形响应全部被分类丢弃（这些错误路径通过注入故障与 mock 分类验证，并非通过制造真实生产故障验证），故障统计可运维而非单一错误计数；有界队列（满则丢样本而不阻塞请求）、worker 异常吞掉、磁盘写失败不影响回合。Kill Switch 采用状态文件 + `KILL` 哨兵（文件生效、下一轮即生效，无需重启），按轮解析、首个匹配优先、fail-safe = off；`auto` 额外要求显式审批、活租约与新鲜心跳，26/26 解析器测试通过；`tools/init_state.py` 不含任何 `--force`——只要 `KILL` 哨兵存在，状态初始化器就拒绝执行（exit 2）且完全无法写入状态，恢复必须先有意删除该哨兵；安装脚本（`tools/install_plugin.sh`）总是用显式 `HERMES_HOME` 调用 `hermes` CLI，所以它改的文件就是它先前备份过的那份文件，环境里另有一个 default/decoy home 也不会收到写入，并且它只在 `mode.json` 不存在时才初始化该文件——重复安装会逐字节保留已有状态（`mode=off`、已跳闸的 breaker 标记或 shadow），也绝不会绕过 `KILL` 哨兵。仓库另内置无依赖的密钥/隐私扫描器（dependency-free secret/privacy scanner），它精确识别自身文件（路径、精确内容哈希以及历史版本的 git blob id），而不是跳过任何包含标记串的文件，因此仅仅引用了模式名的文件仍会被扫描；allow list 只豁免那一处具体的正则匹配——同一行别处出现合成标记或 `REDACTED` 一词，不再豁免旁边的真实形状密钥（有专门的对抗性测试正好覆盖这一点）；私钥模式要求在 PEM header 之后确实跟着 key material 才报，而不是对每一次提及都报警，`.pem` 文件同样会被扫描；遇到超大对象时报告为不完整扫描并以 exit 3 退出，而不是声称干净，除非操作者显式白名单。GitHub Actions 在每次 push 上运行全部四个测试套件、状态初始化 dry run 与密钥/隐私扫描，且无需配置任何 secret。
- **Provider 端到端验证**：主路由 DeepSeek V4.1 Flash 在生产路径上验证（文本、推理、真实工具调用、多步 tool loop、编码/调试、错误路径），模型身份与上下文元数据以 provider 上报的 usage/session 元数据核对；备选路由 MiMo V2.6 Pro 通过 6 项验证矩阵（文本、推理、单次工具调用、多步工具循环、编码/调试、错误处理含 401/429/timeout/5xx 分类，均为注入故障与 mock 分类验证），未验证项明确标注为未验证。
- **可观测性、样本隔离与前瞻设计**：统计工具输出路由占比、置信度分位数（mean/median/p10/p90）、概率间距分布、JEV 延迟（mean/p50/p95/max）、错误计数、隐私回退数，以及任务类别与 Routing Dossier 输入规模分组（routing-input-size proxy，非会话上下文规模）；真实样本与测试样本按 session 来源结构性隔离，测试流量不会被计入生产统计。另完成网关 Agent 生命周期与会话隔离的并发分析（每会话一个 agent、同会话轮次串行、不同会话相互独立），结论为"设计上安全、尚未做并发实测"；并完成 prompt-cache-aware 路由设计（sticky routing + 迟滞，把切换成本显式建模为双方 prompt cache 失效、运行时重建与跨 provider 推理产物不兼容，逐轮切换被视为成本而非特性）；其中 cache 状态与 cache 规模属于仍需采集的运行时信号（future work），当前尚未实测。

## 一句话项目介绍

为 Hermes Agent 构建的隐私保护自适应多模型路由层，以单钩子零核心改动接入，通过 Shadow 模式在真实流量上完成决策采集与验证，并以数据最小化、fail-open 与可回退的运行时 kill switch 保证 Agent 回合安全。

## 精简版 bullet（3 条）

- 为 Hermes Agent v0.21.5 设计并落地隐私保护的多模型自适应路由层（Shadow Router），通过单个 `pre_api_request` 插件钩子接入 TypeSafe JEV 决策服务，在 DeepSeek V4.1 Flash 与 MiMo V2.6 Pro 之间输出逐轮路由决策，零核心改动、无代理层。
- 以 Production Shadow 方式在真实消息网关流量上验证：决策被真实采集与统计、实际执行模型保持不变；完成本地确定性脱敏 + Routing Dossier 数据最小化 + Local Privacy Fallback，telemetry 为 content-free 且字段白名单由测试强制。
- 建立完整可靠性体系：全路径 fail-open 错误分类（timeout / HTTP 401、429、5xx / 畸形响应）、状态文件 + `KILL` 哨兵的运行时 kill switch（按轮解析、fail-safe = off）、真实/测试样本隔离统计；39/39 离线检查通过（live 组需 RUN_LIVE_TESTS=1 显式开启，开启后 46/46）、26/26 kill switch 检查通过，另有 secret-scanner 控制项 24/24 与 installer 36/36 两个套件通过。

## 完整版 bullet（5 条）

- 独立设计并落地面向 Hermes Agent v0.21.5 的隐私保护自适应 LLM 路由层（Shadow Router），通过唯一一个 `pre_api_request` 插件钩子接入 TypeSafe JEV 决策服务（`POST /v1/systemone`，choice 问题），在快速低成本模型 DeepSeek V4.1 Flash 与高能力模型 MiMo V2.6 Pro 之间给出逐轮路由决策，并记录决策服务返回的具体模型版本以保证可复现；集成零核心改动、每轮仅首次调用触发一次。
- 落地 Production Shadow 发布机制，验证 "Production Shadow 已验证 / validated in shadow mode"：路由决策在真实消息网关上被完整采集，实际执行模型保持不变；记录同时保存实际执行模型与未来自动模式会选择的模型，使 Shadow 不变量可逐条审计，先回答"决策是否合理"，再回答"执行是否安全"。
- 构建隐私优先的数据通路：确定性本地正则脱敏（私钥、带前缀 API Key、Bearer Token、`key=value` 密钥、邮箱、IPv4/IPv6、`user@host`、长随机串），Dossier 仅含当前轮脱敏截断文本与布尔任务/风险标记，排除历史、记忆、工具输出与凭证；检出不可安全脱敏内容时整轮不发出并记录 Local Privacy Fallback；telemetry 字段白名单由测试强制，凭据与请求头永不落日志。
- 实现 fail-open 与可逆治理：超时、HTTP 401/403/429/5xx、连接/DNS/TLS 失败与各类畸形响应全部被分类丢弃（通过注入故障与 mock 分类验证，未制造真实生产故障），Agent 回合不受影响；运行时 kill switch 采用状态文件 + `KILL` 哨兵，按轮解析、首个匹配优先、任何异常一律解析为 `off`，`auto` 需显式审批 + 活租约 + 新鲜心跳，26/26 解析器测试通过；状态初始化器没有任何 `--force`，只要 `KILL` 哨兵存在就拒绝执行（exit 2）且无法写状态，恢复需先删除哨兵。
- 建立端到端验证与可观测体系：主路由生产路径验证（文本、推理、工具调用、多步 tool loop、编码/调试、错误路径），备选路由通过 6 项能力矩阵验证；统计工具输出路由占比、置信度分位数、概率间距分布、延迟分位数、错误计数与隐私回退，并按 session 来源结构性地隔离真实样本与测试样本；同步完成并发隔离分析（分析结论明确标注为未做并发实测）与 prompt-cache-aware 的 sticky routing + 迟滞设计。

## 面试 60 秒项目介绍

我做的是一个给 Hermes Agent 用的多模型自适应路由与可靠性系统。背景痛点是：Agent 跑长工具调用循环时，单一模型要么让简单轮次付高能力模型的代价，要么让复杂轮次拿到浅结果；而把路由放进请求路径的第三方方案，会把完整 prompt 和凭证暴露出去，还多一个单点故障。我的做法是在 Hermes Agent v0.21.5 上只用唯一一个 `pre_api_request` 观察者钩子接入 TypeSafe JEV 决策服务，每轮只发一次判断，Dossier 里只有当前轮脱敏并截断的文本和布尔任务、风险标记，不含历史、记忆、工具输出和凭证。关键工程决策有三个：第一，先做 Production Shadow，只采集决策、不改执行模型，用真实流量验证判断质量，记录里同时保存真实执行模型和"未来会自动选择谁"；第二，整条路由链路 fail-open，超时、401、429、5xx 和畸形响应全部被分类丢弃，这些错误路径用注入故障与 mock 分类验证，没有靠制造真实生产故障；同时真实流量里确实出现过一次真实的 JEV 路由超时，那一回合回复照常送达；第三，运行时 kill switch 用状态文件加 KILL 哨兵，按轮解析、异常一律解析为关闭，并由 26 项测试锁定；状态初始化器没有任何 `--force`，只要 `KILL` 哨兵在就拒绝执行、完全写不了状态，恢复必须先删哨兵。可用路由也不是硬编码常量，而是靠显式配置 `JEV_AVAILABLE_ROUTES` 声明，公开默认为空，未配置任何路由时记录里的 `would_execute` 是 `null`。目前成果是 Production Shadow 已验证，39/39 离线检查通过（live 组需 RUN_LIVE_TESTS=1 显式开启，开启后 46/46）、26 项 kill switch 检查通过，另有 secret-scanner 控制项 24/24 与 installer 36/36，备选模型过了 6 项验证矩阵。自动切换仍只在设计阶段，没有启用；并发隔离完成了分析，还缺一次实测，我不会把它讲成已经完成。

## STAR 结构项目讲法

### Situation

Hermes Agent 长期以单一模型跑多轮工具调用循环：简单轮次被高能力模型按高价与高延迟计费，复杂轮次又容易被快速模型草草带过；人工切换体验差，还会打断上下文与 provider 侧 prompt cache。与此同时，业界常见的做法是在请求路径上放一个第三方路由代理，这意味着完整 prompt 与凭证要离开本机，并且新增一个能把 Agent 打挂的单点故障。

### Task

在不修改 Agent 核心、不引入请求路径代理的前提下，建立一条可评估、可回退、隐私可控的多模型路由层：决策服务只能看到最小化的脱敏信息；任何路由故障都不得影响用户回合；自动改变执行模型必须在真实流量上被证明合理之前保持关闭。

### Action

- 以单个 `pre_api_request` 观察者钩子集成，钩子恒返回 `None`，不注入上下文、不修改请求；每轮仅在首次 LLM 调用时决策一次。
- 构建 Routing Dossier：当前轮脱敏后截断文本 + 布尔任务/风险标记 + 可用路由，结构性排除历史、记忆、工具输出与凭证；脱敏为确定性本地正则，离线无网络。
- 实施 Production Shadow 发布：先只采集决策与统计，执行路径完全不动；记录 `actual_model` 与 `would_execute` 以核对 Shadow 不变量；路由可用性由显式配置 `JEV_AVAILABLE_ROUTES` 声明（公开默认为空，未知名称被忽略），未配置任何路由时 `would_execute` 为 `null`，路由需操作者按验证结果显式启用。
- 设计 fail-open 全路径容错：错误按 timeout / `urlerror_*` / `http_<code>` / `malformed_*` 分类；有界队列满则丢样本；状态解析异常一律为 `off`。
- 实现运行时 kill switch：状态文件 + `KILL` 哨兵、按轮解析、fail-safe = off；`auto` 需显式审批 + 活租约 + 新鲜心跳；状态初始化器无 `--force`，`KILL` 哨兵存在时拒绝执行（exit 2）且无法写状态，恢复需先删除哨兵。
- 建立 provider 验证矩阵与样本隔离统计：主路由按生产路径验证，备选路由逐项验证并标注未验证项；真实/测试样本按 session 来源结构性分离。
- 完成并发隔离分析与 prompt-cache-aware 路由设计（sticky routing + 迟滞），并明确其"尚未实测"的状态。

### Result

- 状态为 **Production Shadow 已验证 / validated in shadow mode**：真实消息网关上决策被采集与记录，每次记录中实际执行模型与网关配置模型一致；路由可用性来自显式配置 `JEV_AVAILABLE_ROUTES`（公开默认为空，未知名称被忽略），未配置任何路由时 `would_execute` 记为 `null`，只有操作者配置之后路由才可执行。
- 路由检查 39/39 离线检查通过（live 分组默认跳过，需 RUN_LIVE_TESTS=1 与凭证显式开启，开启后 46/46）；kill switch 解析 26/26 通过；secret-scanner 控制项 24/24 与 installer 36/36 两个套件通过，CI 每次 push 都运行全部四个套件、状态初始化 dry run 与密钥/隐私扫描，无需配置任何 secret。
- Fail-open 通过注入故障与 mock 分类验证（401 / 429 / 5xx / timeout / 畸形响应），并非造成真实生产故障；同时真实流量中出现过一次真实的 JEV 路由超时，该回合回复正常送达并在 telemetry 中被正确分类。
- 隐私侧：本地确定性脱敏与 Local Privacy Fallback 已实现并通过测试；telemetry 为 content-free，字段白名单由测试强制。
- 备选模型通过 6 项验证矩阵（文本、推理、单次工具调用、多步工具循环、编码/调试、错误处理含 401/429/timeout/5xx 分类）。
- 可观测体系上线：路由占比、置信度分位数、概率间距分布、延迟分位数、错误计数、隐私回退，以及任务类别 / Routing Dossier 输入规模分组（routing-input-size proxy，非会话上下文规模），且真实与测试样本结构性隔离。
- 明确边界：自动切换处于设计阶段、未启用；并发隔离仅完成分析，尚未做并发实测；不发布任何成本、性能或规模量化结论。

## English Resume Version

### Project entry (2–3 lines)

Privacy-preserving adaptive LLM routing layer (Shadow Router) for Hermes Agent v0.21.5, integrated through a single `pre_api_request` plugin hook. It asks a TypeSafe JEV decision service for a per-turn route choice between a fast/cost-efficient model (DeepSeek V4.1 Flash) and a high-capability model (MiMo V2.6 Pro), then validates the decision quality in shadow mode on live messaging-gateway traffic while the executing model stays unchanged. Built around data minimisation, fail-open behaviour and a reversible runtime kill switch, with zero core modifications.

### One-line pitch

Built a privacy-preserving adaptive multi-model routing layer for Hermes Agent that collects and validates routing decisions in production shadow mode and can never break an agent turn.

### 3 concise bullets

- Designed and built a privacy-preserving adaptive LLM routing layer for Hermes Agent v0.21.5 using a single `pre_api_request` plugin hook and a TypeSafe JEV decision service, producing per-turn route decisions between DeepSeek V4.1 Flash and MiMo V2.6 Pro with no core patch and no routing proxy.
- Shipped and validated Production Shadow on a live messaging gateway: decisions are recorded while the executing model stays unchanged; the privacy path uses deterministic local redaction, a minimal Routing Dossier and a Local Privacy Fallback, with content-free telemetry enforced by a tested field allow-list.
- Engineered the reliability envelope: full-path fail-open error classification (timeout, HTTP 401/429/5xx, malformed responses) verified through fault injection and mocked error classification, a file-based runtime kill switch resolved per turn with fail-safe off, and real/test sample separation in reporting; 39/39 offline checks; 46/46 with the live routing group enabled via RUN_LIVE_TESTS=1; 26/26 kill-switch checks, plus two more suites (secret-scanner controls 24/24 and installer 36/36).

### 5 full bullets

- Architected and implemented an adaptive LLM routing layer for Hermes Agent v0.21.5 that consults a TypeSafe JEV decision service (`POST /v1/systemone`, choice question) once per turn through a single observer hook, choosing between a fast/cost-efficient route (DeepSeek V4.1 Flash) and a high-capability route (MiMo V2.6 Pro) while recording the concrete model revision behind the alias for reproducibility; integration adds no core patch, no proxy and no parallel runtime.
- Established the Production Shadow rollout (status: validated in shadow mode), collecting real routing decisions on a live messaging gateway while leaving the executing model untouched; each record stores both the actual executing model and what an automatic mode would have chosen, making the shadow invariant auditable before any autonomy is enabled.
- Built the privacy-critical data path: deterministic local regex redaction (private key material, prefixed API keys, bearer tokens, `key=value` secrets, email, IPv4/IPv6, `user@host`, long opaque tokens), a Routing Dossier limited to the current turn's redacted and truncated text plus boolean task/risk flags, and a Local Privacy Fallback that sends nothing when unsendable material is detected; telemetry is content-free with a test-enforced field allow-list, and credentials and headers are never logged.
- Implemented full-path fault tolerance and governance: timeout, HTTP 401/403/429/5xx, connection/DNS/TLS failures and every malformed-response variant are classified and dropped so a turn behaves as if the plugin were absent, and these error paths were verified through fault injection and mocked error classification rather than by causing real production failures (one genuine routing timeout did occur in production traffic and the reply was delivered normally); the runtime kill switch is a state file plus a `KILL` sentinel resolved per turn with fail-safe off, and automatic mode additionally requires explicit approval, a live lease and a fresh heartbeat (26/26 resolver checks pass); the state initialiser carries no `--force`, so while the `KILL` sentinel exists it refuses (exit 2) and cannot write state at all, and recovery requires deliberately deleting the sentinel first. The repository also ships a dependency-free secret/privacy scanner that identifies its own file precisely (path, exact content hash and the git blob ids of its historical versions); its allow list exempts only that specific regex match, so a synthetic marker or the word `REDACTED` elsewhere on the same line does not exempt a real-shaped secret beside it (adversarial tests cover exactly that), and a file that merely quotes a pattern name is still scanned; the private-key pattern requires key material after the PEM header instead of reporting every mention, and `.pem` files are scanned; oversized objects are reported as an incomplete scan and the tool exits 3 instead of claiming clean unless the operator explicitly whitelists them. The installer always invokes the `hermes` CLI with an explicit `HERMES_HOME`, so the file it edits is the file it backed up, and it seeds `mode.json` only when that file is absent, so re-running preserves an existing state (mode=off, a tripped breaker flag or shadow) byte-for-byte and never bypasses a `KILL` sentinel. GitHub Actions runs all four suites, the state-initialiser dry run and the secret/privacy scan on every push, with no secrets configured.
- Delivered end-to-end provider validation and observability: the primary route was validated on the production path (text, reasoning, real tool calls, multi-step tool loop, coding/debugging, error paths) and the alternative route passed a six-item capability matrix (text, reasoning, single tool call, multi-step tool loop, coding/debugging, error handling incl. 401/429/timeout/5xx classification verified through fault injection and mocked error classification); reporting covers route shares, confidence percentiles, probability-margin distribution, latency percentiles, error counts and privacy fallbacks, plus Routing Dossier size buckets (a routing-input-size proxy, not the conversation-context size), with structurally separated real and test samples; route availability is explicit configuration rather than a hard-coded constant (`MIMO_AVAILABLE` is gone, availability comes from `JEV_AVAILABLE_ROUTES` whose public default is empty, and `router.config.available_routes()` ignores unknown names), so with nothing configured the shadow record's `would_execute` is `null` instead of naming a provider and a route only counts as executable once the operator configures it after validation, a deployment-availability surface that also prepares the configuration surface for future routes, while actually adding a route still requires extending the criteria, telemetry, simulation and tests for that route; also produced a concurrency-isolation analysis of the gateway's agent lifecycle and a prompt-cache-aware sticky-routing design whose cache-state and cache-size inputs are runtime signals still to be collected (future work, not yet measured).

### 60-second spoken pitch

I built a multi-model adaptive routing and reliability layer for Hermes Agent. The problem: an agent that runs long tool-calling loops on one model always pays the wrong price, and the usual fix places a third-party router in the request path, which means full prompts and credentials leave the host and you gain a new single point of failure. So I integrated a routing decision service through one `pre_api_request` plugin hook: one decision per turn, built from a dossier that contains only the current turn's redacted, truncated text plus boolean task and risk flags, never history, memory, tool output or credentials. Three engineering decisions mattered most. First, shadow first: decisions are collected and recorded while the executing model stays exactly as configured, so decision quality is proven on real traffic before anything acts on it. Second, the whole routing path is fail-open: timeouts, 401s, 429s, 5xx and malformed responses are classified and dropped - verified through fault injection and mocked error classification rather than by causing real production failures - and when one genuine routing timeout occurred in production traffic the reply was still delivered normally. Third, the runtime kill switch is a state file plus a `KILL` sentinel, resolved every turn with fail-safe off, locked down by 26 checks. Today the status is validated in shadow mode: 39/39 offline checks; 46/46 with the live routing group enabled via RUN_LIVE_TESTS=1; 26/26 kill-switch checks, plus a secret-scanner control suite (24/24) and an installer suite (36/36); and the alternative provider passed a six-item validation matrix. Route availability is explicit configuration rather than a hard-coded constant, so with nothing configured the shadow record's would_execute is null, and while the KILL sentinel exists the state initialiser refuses and cannot write state at all. Automatic switching is designed but not enabled, and concurrency isolation is analysed but not yet empirically tested — I state that boundary plainly rather than implying it works.

## 使用注意

以上表述严格对应项目当前完成度：自动切换（Auto）尚未启用，Shadow 模式不改变实际执行模型，所有未实测项（如并发隔离实测）已显式标注；后续若积累足够的真实流量数据，再补充量化指标。
