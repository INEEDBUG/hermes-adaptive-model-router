# JEV Shadow Router 技术面试问答笔记

这是一份口述笔记：每节先给一段可以照着说出口的参考回答，再给必要的事实边界。  
所有数字只来自离线测试与真实验证记录（39/39 离线检查（live 组 opt-in 后 46/46）、26/26 kill-switch checks、secret-scanner controls 24/24、installer 36/36、v0.2.0 人类回合来源边界 37/37、6 项 provider 矩阵），不含 benchmark 或成本节省结论。  
`>` 开头的是边界声明，说明哪一条是读源码分析出来的、哪一条是实测过的 —— 面试时说清这条，比多抛一个数字更有说服力。

> 测试默认完全离线：live 组只有显式设置 `RUN_LIVE_TESTS=1` 时才发起真实调用（本机恰好有凭据不会触发）。offline by default; the live group only runs with `RUN_LIVE_TESTS=1`, so a credential present on the machine never triggers a third-party call.  

> CI 每次 push 都跑全部四个套件、状态初始化 dry run 与密钥/隐私扫描，且不配置任何 secret；扫描器精确识别自身文件（路径 + 精确内容哈希 + 历史版本的 git blob id），所以只是引用了模式名的文件照样会被扫描；allow list 只豁免那一处具体的正则匹配 —— 同一行别处出现合成标记或 `REDACTED` 一词，并不能豁免旁边的真实形状密钥（有对抗性测试正好覆盖这一点）；私钥模式要求在 PEM header 之后确实有 key material 才报，而不是对每一次提及都报警，`.pem` 文件同样会被扫描；超大对象会被报成不完整扫描并以 exit 3 退出，除非显式白名单。  

## 1. 为什么需要 JEV（为什么不自己做规则/分类器）？

需要的是一个决策服务，但真正稀缺的不是"它会选哪个模型"，而是"它对自己的选择有多确定"。  
规则或自研分类器能做到确定性、可审计，但需要标注数据、需要长期维护，而且规则一多就变脆 —— "帮我看看这段日志"到底算 debugging 还是闲聊，本身就是一个模糊判断。  
JEV 以 `choice` 问题返回 choice、confidence 和 probabilities，这个校准过的置信度是下游所有机制的输入：shadow 阈值、hysteresis 条件、低置信度时偏向 capable route 的兜底。  
而且它是严格 advisory 的：决策服务挂了不会影响 turn，这一点比自己写一套分类器把托管逻辑塞进 agent 里要安全得多。  

> 这不是说自研分类器做不到，而是说在 shadow 阶段我们更看重"可校准的置信度 + 可替换的决策服务"这一层抽象，JEV 只是当前这一层的实现。  

## 2. 为什么不直接一直用最强模型？

因为在长的 tool-calling loop 里，每一轮都在某个地方付错价钱。  
状态查询、跑一条命令这种 turn 用最强模型是纯溢价，付的是 latency 和 price。  
反过来全用便宜模型，在架构设计、多文件 debug 这类任务上会产出浅层结果，需要返工，而返工比一开始就贵得多。  
所以我们的原则是 lowest sufficient model：由任务需求决定模型，而不是由习惯决定。  

> 没有任何 cost-saving 或 latency 数字：真实 shadow 样本故意很小，只发布分布，不发布 headline 数字。任何"省了百分之多少"的说法在这个项目里都是编的。  

## 3. 为什么先做 Shadow 而不直接自动切换？

因为路由层同时提出了两个命题："我的决策是对的"和"照着我的决策执行是安全的"。只有第一个能在不碰生产的前提下被评估。  
Shadow 模式下插件只注册一个 observer hook（`pre_api_request`），hook 永远返回 `None`，所以请求内容一个字节都不变；决策被记录，执行模型仍由 gateway 自己的配置决定，生产路径还是 DeepSeek V4.1 Flash。  
运行模式也不是由环境变量决定的：**状态文件 `mode.json` 是权威**，每轮解析一次；文件缺失、损坏或不可读一律解析成 `off`（fail-safe），所以单独设一个 `ROUTER_MODE=shadow` 并不会打开采集。  
记录里同时有 `actual_model`（真正执行的是谁）和 `would_execute`（未来的 auto 会选谁），所以任何决策规则都能事后离线评估，不用先切一次生产。可用性也不再是代码里的常量：`MIMO_AVAILABLE` 已经删掉，一条路由是否可执行由显式配置 `JEV_AVAILABLE_ROUTES`（如 `deepseek_flash,mimo_pro`）决定，公开默认为空，`router.config.available_routes()` 会忽略未知名称 —— 什么都没配时 `would_execute` 是 `null`，而不是指名某个 provider，这条规则对未来新增路由同样成立：`JEV_AVAILABLE_ROUTES` 让部署可用性显式化，并为未来路由准备了配置面；真正新增一条路由仍需要为其扩展 criteria / telemetry / simulation / tests。  
这就是 observable before automatic：先统计，再规则，最后才是自主。  

> auto 目前是 design stage，未启用；启用需要代码自己检查的显式 approval flag。  
> 这一版把状态文件里记录的 `auto` 降级成 `shadow`，并往 mode audit log 写一条 `auto_not_implemented`，所以 telemetry 和运维视图都不会显示出 auto 跑过。  
> 安装脚本 `tools/install_plugin.sh` 把 `JEV_ROUTER_ROOT` 持久化进 Hermes 的 `.env`，对 `plugins.enabled` 是 merge 而不是 replace —— 已有插件条目会保留；而且它总是用显式 `HERMES_HOME` 调用 `hermes` CLI，所以它改的文件就是它备份过的那份文件（环境里另有一个 default/decoy home 也不会收到写入）；备份紧贴在写入之前：`config.yaml.bak.<timestamp>` 在第一次 `hermes config set` 之前、`.env.bak.<timestamp>` 在 append 之前，备份不成功就直接拒绝修改该文件。  
> 安装脚本只在 `mode.json` 不存在时才初始化它：重复安装会逐字节保留已有状态（`mode=off`、已跳闸的 breaker 标记或 shadow），并且绝不会绕过 `KILL` 哨兵。  

## 4. 为什么不能每轮随便切模型？

因为切换 provider 不是免费的。  
切过去，新的 provider 要重新处理未缓存的前缀，旧的 provider 缓存直接变冷 —— prompt cache 在两边同时失效。  
换模型还会重建 provider client 和 runtime、重新解析 reasoning/compression 配置，丢掉这个 session 缓存的 agent。  
更麻烦的是 reasoning artifact：有些 provider 输出的推理产物在另一个 provider 上无法重建或重放，带着这类 artifact 的线程不能中途搬家。  
所以 router 必须 sticky：confidence ≥ 0.75、margin ≥ 0.30、challenger 连续赢 ≥ 2 轮、cooldown ≥ 5 轮或 ≥ 10 分钟、context < 20k tokens、且 task class 确实变了 —— 全部满足才切，否则维持现状。  

> 这套规则在文档里是 informative 的，尚未实现，也没有做过负载测试。per-turn flipping 是成本，不是特性。  
> 其中 `context < 20k tokens` 依赖一个目前并未采集的信号：`dossier_token_estimate` 只是 Routing Dossier（路由输入）大小的代理，真实 conversation context 与 prompt cache 大小属于运行时信号，需要 future auto 版本单独采集 —— 现在没有采集，所以这个阈值目前还只是设计值。  

## 5. Prompt Cache 对成本有什么影响？

Provider 会缓存对话前缀，而切换会让这个缓存失效 —— 新 provider 全量重算，旧 provider 的缓存白付。  
在生产使用里观察到的量级关系是：cached prefix 在每轮 input 中占绝对主导，cached input tokens 比 fresh input tokens 高一到两个数量级。  
也就是说，一次切换带来的重算成本，可能比这一轮路由本身省下的还多。  
这正是 hysteresis 里 `context < 20k tokens` 和 cooldown 条件的由来：长线程不动，刚切过的不再切 —— 这里的 context 大小同样是尚未采集的运行时信号。  

> 这里说的是分布与量级关系，不是某个具体金额或节省百分比。上面那个阈值里的 context 大小是尚未采集的运行时信号 —— `dossier_token_estimate` 只量 dossier 本身，并不度量 agent 的对话上下文。  

## 6. JEV 挂了怎么办？

Fail-open，加上分类。每个失败都被归类：`timeout`、`http_401`、`http_429`、`http_5xx`、`urlerror_*`，以及契约类错误 `malformed_response`、`malformed_answer_type`、`malformed_unknown_choice`、`malformed_missing_probabilities`、`malformed_confidence`。  
分类不是为了让日志好看：`http_401` 要轮换凭据，`http_429` 要退避，`urlerror_*`/`timeout` 是运维问题，而 `malformed_*` 是契约漂移、需要改代码 —— 处理动作完全不同，一个笼统的错误计数没法指导任何一件事。  
失败只是被记录：client 不重试、不阻塞、不抛异常；有界队列（size 8）满了就丢样本，而不是让 turn 等；worker 线程里的异常就地吞掉。  
实测覆盖了 endpoint 不可达、无效凭据、malformed response，生产里也真实出现过一次 routing timeout，用户侧无任何可感知影响。  

> 那是"观察到过一次真实的 routing timeout"，不是压测出的失败率。  

## 7. 为什么采用 Fail-open？（以及为什么隐私是 fail-closed）

一个能把 agent 弄挂的路由层，比没有路由层更糟 —— 决策服务不在 critical path 上，它只是 advisory。  
所以可用性上 fail-open：任何未预期状态都解析成不影响 turn 的行为，最差情况是"插件像没装一样"。  
隐私是另一回事：如果 redaction 碰到了自己无法确信能清洗干净的材料（比如 private key block），继续发出去就是把风险外置给第三方。  
所以 private key material 触发 Local Privacy Fallback：什么都不发，本地写一条 `route = LOCAL_PRIVACY_FALLBACK` 的记录，turn 用 gateway 配置的模型照常进行。  
两条策略是刻意分开的 —— 可用性 fail-open，隐私 fail-closed。  

> fail-closed 只作用于"是否外发"，不作用于 turn 是否继续；用户不会因为隐私策略而丢掉这一轮。  

## 8. 如何保护隐私？

Data minimisation：dossier 只包含当前 turn，确定性 redaction 之后截断到 1200 字符，加上 boolean 的 requirement flags（tool use、shell、coding、debugging、research、long context）和 risk flags（destructive action、production change）。  
结构性保证比"记得过滤"可靠：dossier builder 的唯一内容入参就是当前 user message，它拿不到 memory、history、tool output 或文件内容。这里要把"到底收到了什么"说准：服务确实会收到经过 redaction、截断到 1200 字符的当前轮文本，加上 boolean 的 requirement / risk flags；它收不到的才是 history、memory、tool output、文件内容和凭据。  
Redaction 是本地、离线、正则的：不用模型、不连网，所以它自己不会成为泄露源，覆盖 PEM private key、前缀 API key、Bearer token、`key=value` secret、email、IPv4/IPv6、`user@host` 和长 opaque token。  
Telemetry 只记录 redaction 的命中次数，不记录内容；字段走固定 allow-list，加一个可能带内容的字段会直接把测试套件跑红。v0.2.0 起再加一层来源边界：只有 human-origin 回合才会走到这一步，platform 白名单本身不算边界（见第 13 节）。  

> 确定性正则 redaction 不是保密保证，也不是 DLP：匹配不到任何模式的 secret 它认不出来，所以这是很强的卫生习惯，而不是合规或保密的承诺。另外最小化不等于不可推断 —— 服务仍然看得到任务形状和请求时序。  

## 9. 如何处理并发 session？

结论来自读 gateway 的实现，不是假设。  
Gateway 每个 session 缓存一个 agent instance（LRU 上限加 idle 淘汰），所以 agent 对象是 session-local 的，而不是跨 session 共享。  
同一个 routing key 的第二条消息进不了正在进行的 turn —— in-flight marker 在任何 `await` 之前就注册了；两个 key 解析到同一个 session 时还会被 per-session turn lease 串行化；不同 session 则可以在各自线程里并发跑，各带自己的 agent。  
由此，一个 turn-scoped 的 model override 只要是发给本 session 自己的 agent、并且总在 `finally` 里 restore，就"by construction"安全；剩下的细节是一个 per-session 的 switch generation token，防止旧的 restore 把新一轮的切换撤销掉。  

> 但这是 analysed、不是 empirically tested：并发切换下的实证测试还没做，所以 auto 保持关闭，隔离要求写在设计文档里，而不是宣称已验证。  

## 10. 为什么没有马上开启 Auto？

三个原因叠加。  
第一，规则还没有数据支撑：真实流量样本很小，hysteresis 阈值必须先用 shadow 统计去评估，而不是靠直觉定。  
第二，安全机制还没实证：并发隔离目前是"推断安全、未在负载下验证"，而 restore 的失败路径正是核心残余风险 —— 这也是为什么 future auto 的 override 要由拥有 `finally` 的 turn 编排层来施加，而不是一个本身就没有 `finally` 的 hook。  
第三，切换本身是负收益风险：cache 失效、runtime 重建、reasoning artifact 冲突都在，没有 sticky 规则就跑 auto，可能比不跑更贵。  
再加上启用 auto 需要显式 approval flag（代码自己检查）+ 活的 lease（`auto_until`）+ 新鲜 heartbeat（120 s），任意一项不满足都自动退回 shadow —— 改一个配置字符串是开不起来自治的。而且这一版对状态文件里记录的 `auto` 是降级处理的：生效模式回落成 `shadow`，mode audit log 里写一条 `auto_not_implemented`，所以 telemetry 和运维视图上都不会出现"auto 跑过"的痕迹。  

> 状态是 designed, not enabled；没有任何"auto 已经跑通"的说法；并发隔离是分析结论，不是负载测试结论。  

> KILL 这条线是硬的：`tools/init_state.py` 现在没有任何 `--force`，只要 `KILL` 哨兵还在，初始化器就直接拒绝（exit 2）并且完全写不了状态 —— 想恢复必须先有意把哨兵删掉。  

## 11. DeepSeek 和 MiMo 如何分工？

按任务需求分，不按品牌分。  
Fast route 承接清晰、常规、对延迟敏感的工作；capable route 承接 ambiguous、深推理、coding、多文件、长 horizon 和质量关键的工作。  
两条路线都是先验证再上线：DeepSeek V4.1 Flash 作为生产路径，在 gateway 的真实流量下验证（文本、reasoning、tool calling、多步 tool loop、coding/debugging、模型身份与 context metadata、错误路径）；MiMo V2.6 Pro 作为 alternative route 走 M1–M6 六项矩阵，在通过验证并由操作者按 `JEV_AVAILABLE_ROUTES` 显式配置之前，它不会被算作可执行路由（可用性来自显式配置，公开默认为空，未知名被忽略）。  
验证只看 provider 自己上报的 usage/session metadata，不看配置字符串 —— 配置说用了谁不算证据。  

> M6 里"provider 不可达"那一支没有声称通过：容器的 host 解析无法在不碰共享网络的前提下隔离，所以只跑了注入式凭据失败和 mock 的分类验证。M4 第一次也不达标（"两步"被一条组合命令满足了），重写成第二步依赖第一步之后才通过 —— 未验证的就报未验证。  

## 12. 这个项目下一步是什么？

先把 G1 走完：积累足够的真实流量样本，用分布去评估 sticky 规则 —— challenger 赢的 margin 分布、task class 与 JEV choice 的相关性、confidence 卡在阈值附近的频率、以及每次切换会付出多少 cache 成本。  
`would_execute` 这个字段就是为此存在的：规则可以在真实 turn 上离线评估，一条都不用真的切（可用路由必须先在 `JEV_AVAILABLE_ROUTES` 里显式配置；没有配置任何路由时这个字段就是 `null`）。  
之后是 G2（设置 approval flag，验证 breaker、lease、heartbeat）、G3（并发实证测试，以及负载下演练 kill switch）、G4（在有界窗口 `auto_until` 内开启 auto，且 sticky routing 生效）。  
在这一版里，状态文件写 `auto` 也不会真的自治：模式先回落成 `shadow`，mode audit log 里记一条 `auto_not_implemented` —— 在 G4 之前，"开关"和"行为"是分开的两件事。  

> 任何一关不满足就停在 shadow；没有时间表承诺，G1 的样本量由真实流量决定；auto 是 designed, not enabled。  

## 13. 人类回合来源边界（v0.2.0）：只有真正的人类回合才能被观测吗？

这一节回答 v0.2.0 让这个项目可以回答的四个问题：为什么 platform 白名单不是隐私边界、为什么来源判定必须是结构性的而不是看文本、fail-closed 对"标签缺失"具体意味着什么、以及在不碰冻结生产系统的前提下这套改动是怎么验证的。

一个 platform 白名单不是隐私边界，只是一个入口开关。钩子在每次 provider 请求前都会触发，而网关自注入的通知、compaction 续跑、background-review fork 和 subagent 回合都会**继承**来源会话的 platform 标签 —— 一次飞书会话的 background review 一样上报 `platform=feishu`。所以"平台在 allowlist 里"根本说明不了这一轮是人类发的；这个混淆是在真实 telemetry 里被诊断出来的，也正是 v0.2.0 要修的东西。
修复是结构性的，不是文本启发式：在**任何 Routing Dossier 构造之前**过两道门 —— 门 1 `platform IN JEV_ALLOWED_PLATFORMS`，门 2（权威条件）`turn_origin == "user"`；任一不满足就拒绝该回合，发生在 `redact → dossier.build → shadow.submit` 之前。**不用消息文本判断来源**：文本是任何人都能写进请求里的，而真正的混淆来自结构（fork / continuation / 通知），不是措辞 —— 拿文本当判据既不可靠，也会把"猜"混进一个本该是确定性的判定里。
`turn_origin` 由核心提供，来自一份最小的 Hermes 集成补丁（`patches/hermes-v0.21.5-turn-origin.patch`，针对 Hermes Agent v0.21.5 构建与测试），取值是一个封闭词表：`user`、`internal_notification`、`compaction_continuation`、`background_review`、`subagent`、`oneshot`、`cron`、`curator`、`api_server`、`unknown`。
"fail-closed" 在这里的含义很具体：来源标签**缺失、为空或 unknown 一律拒绝**，绝不默认成 `user`。最直接的后果是 —— 如果某次 Hermes 升级把这份集成弄丢了，插件收不到 `turn_origin`，于是它拒绝一切回合、路由直接停摆：宁可什么都不观测，也不会在来源未知的情况下把当前轮文本交给第三方决策服务。
验证完全离线或脱敏，没碰冻结的生产系统：37 项离线矩阵（A–G 组：双门矩阵、fail-closed 路径、按 turn 计数、并发隔离、content-free telemetry、异常检测器、记录里的来源字段）；并发隔离测试让人类回合与 background 回合并行跑；content-free telemetry 审计确认拒绝记录只写 date / platform / turn_origin / reason / count，且写入路径没有 message 参数；脱敏生产 smoke 里人类回合得 1 条路由决策，subagent / internal notification / background review / 缺失或 unknown 来源都是 0，并发 user-background 隔离与 content-free telemetry 均 PASS。上游漂移由 `tools/check_turn_origin_patch.py` 守着：版本目标、补丁标记、插件里的两道门、离线 fail-closed 证明，任一不符即 exit 3，路由器保持 fail-closed/关闭，直到补丁重新应用。

> The router only observes human-origin turns. Internal, subagent, background and continuation turns are rejected before Routing Dossier construction.

> Shadow routing logic remains plugin-based, while strict human-turn provenance requires a minimal Hermes v0.21.5 turn_origin integration patch. 这条要主动讲：v0.1.x 的说法是"零核心补丁"，那是当时的范围，而严格的人类回合来源需要这份最小补丁 —— 说清楚比让面试官自己去发现更有说服力。

> 生产仍是 shadow：自动切换未实现且关闭，从未启用过自动路由。这个边界不是在生产系统上验证的 —— 它靠离线矩阵加一次脱敏 smoke 验证，没有对运行中的 gateway 动手。

> 不声明路由准确率、成本节省或任何自动切换收益，也不声明 shadow 观测之外的任何生产影响。

> Built and tested against Hermes Agent v0.21.5. Hermes Agent is a Nous Research project; this repository is an independent plugin project with no affiliation to it.

## 14. （附加）如果让你从零再做一次，你会改进什么？

我会更早把并发测试写出来，而不是先写并发分析：读源码得出"by construction 安全"是有价值的，但真正卡住 auto 的恰好是那个没做的实证测试，测试先行会让 rollout gate 的边界更早明确。  
我会把样本来源标记放进写入路径，而不是靠 `turn_id` 前缀加 session DB 反查来区分真实流量和测试流量 —— 现在这套分离是可审计的，但统计工具会因此简单很多。  
我还会在第一天就把 hysteresis 的观测字段（margin 分布、switch 成本估算）定下来，因为"切一次值不值"这个问题，事后再补指标会比一开始就采集难。  

> 这是设计取舍的复盘，不是对当前实现的否定：在 shadow 阶段，先保证"决策被忠实记录、执行不受影响"这个顺序是对的。  
