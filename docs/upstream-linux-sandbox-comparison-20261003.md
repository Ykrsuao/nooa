# 原项目 Linux 沙箱与当前 ACP 沙箱对比

> 历史对比：本文记录加入 `code` 模式之前的 ACP 行为，文中的“当前”指当时的
> strict 实现。后续已按用户选择加入两端代码沙箱模式，并为 Zed 启用；现行
> 行为请参阅 [使用说明](acp-sandbox.md) 与 [最终验收](sandbox-parity-verification-20261003.md)。

核查日期：2026-10-03。原项目基准为本地 `origin/main` 已取得的 NVIDIA-NeMo/labs-OO-Agents 提交 [`2ff4485ee6c1dfcfa6956716f3df9f5290ef617d`](https://github.com/NVIDIA-NeMo/labs-OO-Agents/commit/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d)，提交日期为 2026-10-01；这里不宣称它是远程最新版本。当前版本指本地工作区。本说明基于源码审阅，没有新增运行测试，也没有修改代码或 Zed 配置。

**原项目 Linux 沙箱也默认禁止执行代码直接联网，但可以配置开放网络，也保留在宿主进程调用工具的通道。当前 ACP 则额外收紧了工具入口，因此搜索能力消失并不是 Windows 独有的问题。** 原项目的 ACP 入口本身没有启用它已有的 Linux 沙箱，必须区分“框架拥有沙箱”和“Zed 使用的 ACP 实际启用了沙箱”。

| 项目 | 原项目 Linux 沙箱 | 当前 ACP 沙箱（Windows / Linux） |
| --- | --- | --- |
| 执行代码直接联网 | `network=False` 默认禁止创建 IPv4/IPv6 网络套接字；显式 `network=True` 可放开。它不是按网站授权的搜索服务。[1][2] | 当前 ACP 采用禁网策略；Linux 明确设置 `network=False`，Windows 未授予网络或 HTTPS 端点。ACP 没有网络开放参数。[6][8] |
| 通过宿主工具搜索 | `self.*` 方法通过代理在宿主进程执行。若应用提供可联网的方法，它可以执行联网操作；worker 的网络限制不会自动约束宿主工具。原版没有因此自动附带一个搜索引擎。[3] | 仅授予工作区文件、隔离命令和 `message` 七个方法，没有网页搜索、网页读取方法。当前两平台都如此。[6] |
| 文件写入保留 | 如果配置 `workspace` 或可写 `FileRule`，直接授予真实目录读写权限；写入会留在该目录，没有自动快照回滚。默认 `workspace=None` 则没有工作区写授权。[1][4] | 文件工具修改真实项目并保留；`run_command` 在临时副本运行，命令改动丢弃。副本有体积限制，并排除 `.venv`、`node_modules` 等。[6][7] |
| MCP 和项目扩展 | 原 ACP 会加载项目技能、库以及客户端转发的 MCP，但它创建的是未启用沙箱的 `CodingAgent`。不能把这些扩展描述成已经受 Linux 沙箱约束。[5] | 沙箱 ACP 创建专门的 `SandboxCodingAgent`，不加载项目 Python 技能、库或 MCP；收到 MCP 请求会拒绝。[6][9] |
| 子进程 | 原网络 seccomp 只针对网络套接字，其他系统调用不在该过滤器的禁止范围；没有当前 ACP 对生成代码创建子进程的专门限制。进程仍受实际安装的文件、网络和资源约束。[2] | Windows 生成代码进程禁止创建子进程；专用命令入口允许受 Job Object 管理的进程树。Linux 新入口同样限制生成代码创建进程，并通过独立命令运行器执行命令。[10][11] |
| 内存、CPU、超时 | 内存与 CPU 上限默认 `0`，即不启用；可选 `RLIMIT_AS`、`RLIMIT_CPU`。代码超时与宿主工具超时分别处理。[1][3] | ACP 显式配置代码 30 秒、宿主方法 120 秒、内存参数 512 MiB、CPU 参数 60 秒。两平台内存及 CPU 计量不同，数值相同不代表限制完全等价；命令有独立预算。[6][12] |
| 无法建立要求的隔离 | 默认 `require=True` 报错；显式 `False` 可删去不可执行的限制并警告。[4] | 当前 ACP 要求原生后端，不会静默回退到宿主机直接执行。[6][9] |

## 对联网搜索的实际含义

当前 Windows 底层其实已经实现了**固定 HTTPS 端点代理**：宿主进程按预先授权的 URL/IP 执行受限 GET，再返回结果；这不是给 worker 任意联网权限，也不是完整搜索服务。当前 ACP 没有为它配置端点，也没有向 Agent 暴露搜索工具。[8][12]

因此，若保持当前 ACP 的隔离方式并恢复搜索，合适的改动是增加明确授权的网页搜索/读取方法；它们在宿主进程执行，需要自己的 URL、网络和返回内容边界。仅切换到当前 Linux ACP 沙箱不能恢复搜索，因为它使用同样的工具清单与禁网策略。[6] 至于关闭沙箱后某次成功搜索究竟用了直接 Python 请求、shell 还是 MCP，需要该次工具调用记录，不能仅凭模型回复确定。

原 Linux 采用 `fork` 继承宿主解释器；当前 Linux 的收紧入口仍建立在该机制上。不能把文件和套接字限制宣传为“宿主凭据内存完全不可见”。[1][11] 本文没有对两个平台作总体安全强弱排名。

## 源码依据

1. 原版 [SandboxConfig 默认值、网络开关、资源及 require 配置](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d/src/nooa/runtime/sandbox/config.py#L40-L128)，[真实路径规则解析](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d/src/nooa/runtime/sandbox/config.py#L203-L242)。
2. 原版 [网络 seccomp 过滤器及 guard 安装](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d/src/nooa/runtime/sandbox/guards.py#L303-L369)。其网络限制针对新建 IPv4/IPv6 socket，不应扩大解释为对所有网络、已有描述符和宿主代理的完整隔绝。
3. 原版 [宿主工具独立超时](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d/src/nooa/runtime/sandbox/executor.py#L370-L382)、[在宿主 Agent 上解析并执行 self 方法](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d/src/nooa/runtime/sandbox/executor.py#L439-L479)。
4. 原版 [require=False 降级处理及真实 workspace 创建](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d/src/nooa/runtime/sandbox/executor.py#L112-L161)。
5. 原版 [ACP 创建 CodingAgent、加载技能和 MCP](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d/packages/nooa-acp/src/nooa_acp/server.py#L401-L426)、[CodingAgent 仅设置 cell_timeout](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d/packages/nooa-cli/src/nooa_cli/coding/agent.py#L193-L195)、[execution_backend 默认为 inprocess](https://github.com/NVIDIA-NeMo/labs-OO-Agents/blob/2ff4485ee6c1dfcfa6956716f3df9f5290ef617d/src/nooa/config/strategy_config.py#L85)。
6. 当前 [七个宿主方法及能力说明](E:/rivon/labs-OO-Agents/packages/nooa-cli/src/nooa_cli/coding/sandbox_agent.py:33)、[Windows / Linux ACP 策略](E:/rivon/labs-OO-Agents/packages/nooa-cli/src/nooa_cli/coding/sandbox_agent.py:91)、[持久文件工具](E:/rivon/labs-OO-Agents/packages/nooa-cli/src/nooa_cli/coding/sandbox_agent.py:155)。
7. 当前 [命令复制快照、运行和返回 changes_discarded](E:/rivon/labs-OO-Agents/packages/nooa-cli/src/nooa_cli/coding/sandbox_agent.py:242)。
8. 当前 [Windows 默认空 HTTPS 授权](E:/rivon/labs-OO-Agents/src/nooa/runtime/sandbox/_windows_policy.py:43)、[仅在配置端点时提供 fetch_https](E:/rivon/labs-OO-Agents/src/nooa/runtime/sandbox/_windows_session.py:119)。
9. 当前 [ACP 创建不同 Agent 的分支](E:/rivon/labs-OO-Agents/packages/nooa-acp/src/nooa_acp/server.py:462)、[拒绝沙箱模式 MCP](E:/rivon/labs-OO-Agents/packages/nooa-acp/src/nooa_acp/server.py:524)。
10. 当前 [Windows 默认子进程限制](E:/rivon/labs-OO-Agents/src/nooa/runtime/sandbox/_win_appcontainer.py:377)、[专用命令会话预算](E:/rivon/labs-OO-Agents/src/nooa/runtime/sandbox/_windows_command.py:106)、[命令启动允许子进程](E:/rivon/labs-OO-Agents/src/nooa/runtime/sandbox/_windows_command.py:265)。
11. 当前 [Linux 生成代码进程过滤器](E:/rivon/labs-OO-Agents/src/nooa/runtime/sandbox/_linux_session.py:25)、[使用 fork 的进程构建](E:/rivon/labs-OO-Agents/src/nooa/runtime/sandbox/_linux_session.py:108)、[Linux 独立命令过滤器](E:/rivon/labs-OO-Agents/src/nooa/runtime/sandbox/_linux_commands.py:39)。
12. 当前 [平台资源语义对照](E:/rivon/labs-OO-Agents/docs/windows-sandbox-policy.md:131)、[固定 HTTPS GET 代理策略](E:/rivon/labs-OO-Agents/docs/windows-sandbox-policy.md:167)。
