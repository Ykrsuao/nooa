# Windows / Linux 代码沙箱统一验收

用户选择的目标是原项目 Linux 模式：生成的 Python 代码在原生沙箱中执行，
终端、技能、库与 MCP 保留为可信宿主工具。本次统一这一运行方式，不把它描述
为完整开发终端隔离，也不声称两个操作系统的底层权限机制完全相同。

## 使用方式

```sh
nooa-acp --model YOUR_MODEL --sandbox auto --sandbox-mode code
```

两端都使用普通 `CodingAgent`，在 dispatcher 调用 `handle` 时注入沙箱策略。
Windows 采用原生 LPAC / Job Object 和显式 `host_tools=True` 代理；Linux
保留原有 fork 执行器及宿主工具代理。没有新增专用网页搜索工具。

- 宿主终端可以联网，命令对真实项目的改动会保存。
- 技能、库与 MCP 按普通 Agent 的规则加载和调用，保留宿主权限。
- `--sandbox-network off` 默认仅限制生成代码的直接联网，不限制宿主工具。
- `--sandbox-network on` 在 code 模式显式开放生成代码的原生网络权限。
- 既有 strict 模式仍可使用，`--sandbox-mode` 默认 strict，以免改变旧启动命令。
- code / strict 分别使用 `acp-code-sandbox-sessions` / `acp-sandbox-sessions`
  历史目录，不自动混用旧会话；初始化失败不会回退到宿主 Python 执行。

## 配置和边界

`SandboxSession(SandboxConfig(...))` 现在支持两端共用的代码隔离配置。
Windows 对不支持的 Linux 直接目录授权、内存/CPU 单位及其他平台专属选项
明确报错，避免静默改变含义。显式 `WindowsSandboxPolicy` 保留原有 native API；
`host_tools=False` 仍是该原生接口的默认值。

可信宿主返回值可以传入 worker；worker 向宿主发出的请求仍使用有界 msgpack
和已声明数据类型，未开放反向 pickle。Windows CLI 依赖暂存只复制已安装的
first-party Python 包源码，不复制项目根目录、凭据或执行 `.pth`。

Linux code 模式保留原 fork 内存和描述符继承行为，不能套用 strict 模式的
描述符关闭验收。Windows 的 AppContainer 回环、异步 I/O 和依赖暂存仍有自身
限制；普通宿主工具的联网不受这些 worker 限制影响。macOS 不在本次支持范围。

## 进一步统一实际行为

用户确认接受底层机制不同，并要求尽可能统一可观察行为后，又补齐以下内容：

| 项目 | 两端共用的行为 |
| --- | --- |
| 超时预算 | 共享配置的代码时限及 `timeout_grace_s` 均生效；ACP 是 30 秒代码时限加默认 2 秒宽限。宿主工具等待时间不消耗代码预算，宿主工具另有 120 秒上限。 |
| 配置校验 | 共享会话创建时固定代码时限；两端均拒绝零、负数、无穷和 NaN 时限。禁用时限用 `None`，宿主工具不限时用 `broker_timeout_s=0`。 |
| 直接创建进程 | managed code 模式均禁止生成代码直接创建或替换进程，仍允许线程及通过宿主终端运行命令。原始 Linux `SandboxedExecutor` 的进程策略保留。 |
| 工具对象 | 通过 `self` 读取的支持数据按快照传递；非数据工具对象保留宿主代理，避免 Linux 把可 pickle 的工具对象复制进 worker，导致状态丢失或命令意外在沙箱中执行。 |
| 取消与恢复 | 取消时结束并等待本轮宿主异步调用及读取任务清理；重启得到空代码命名空间，禁用恢复时不启动替代 worker。已发生的宿主工具效果不会回滚。 |
| 常用代码名称 | Windows 显式提供 Linux CodingAgent 中的 15 个常用类型，包括 `Path`、`Context`、配置和结果类型；不自动复制项目全局状态或整个环境。 |
| 给模型的说明 | 两端明确区分代码限制与宿主工具权限，说明快照修改、数据传递、超时和恢复规则。 |

进一步统一不改变剩余的平台边界：Windows 回环联网、依赖暂存、直接文件授权、
内存/CPU 计量与 Linux 不同；Linux fork 仍继承原内存和描述符。宿主 terminal、
技能和 MCP 保持用户所选的宿主权限。常用类型已经对齐，但不承诺任意第三方
Python 包、宿主配置辅助函数或 Python 作者标记自动出现在 Windows worker 中。

## 验证记录

本次使用 Windows Python 3.12.13、WSL Ubuntu Python 3.12.3，均通过 uv 运行。
ACP 协议测试使用 FakeLLM 驱动真实原生 worker、真实宿主 shell 和本地 HTTP
监听器，不访问模型 API，也不依赖外部搜索服务。

| 验证 | 结果与证据 |
| --- | --- |
| 共用配置与拒绝不支持的语义 | `test_portable_session.py` 15 passed |
| Windows API / session 回归 | 首轮 159 passed；唯一失败为旧字段清单断言，补齐新字段后 4 项相关测试通过；[修复后记录](../logs/windows-policy-parity-20261003.xml) |
| Windows 原生网络 | 13 项配置/能力/默认拒绝/回环/清理合同通过；普通 LPAC 和 managed session 均真实 HTTPS 成功；[合同](../logs/windows-network-contracts-20261003.xml)、[实机](../logs/windows-network-20261003.xml) |
| 普通 Agent 宿主工具 | 4 passed：真实 shell 写回、read/replace、repo、MCP mock、Waiting、取消及恶意消息拒绝；[记录](../logs/lpac-host-tools-windows-20261003.xml) |
| 原 exact 工具模式 | 6 passed；[记录](../logs/lpac-exact-host-parity-regressions-20261003.xml) |
| Windows CLI 依赖暂存 | 38 项合同及 1 项实际 LPAC 导入通过；[合同](../logs/windows-cli-staging-green-20261003.xml)、[实机](../logs/windows-cli-staging-native-20261003.xml) |
| WSL 启动脚本 | 参数、env、Windows/WSL 路径与资源 URI 往返、实际适配器及参数转发通过，0 模型调用；[记录](../logs/wsl-launcher-verification-20261003.json) |
| Windows ACP 完整协议流程 | 1 passed，164.39 秒；真实文件写回、worker 禁网时宿主 HTTP、取消后复用、历史恢复后新一轮执行；[记录](../logs/acp-code-windows-final-20261003.log) |
| WSL Linux ACP 相同协议流程 | 1 passed，82.85 秒；使用本项目源码和原生 Linux worker；[记录](../logs/acp-code-wsl-final-20261003.log) |
| ACP CLI / code / strict 单元回归 | 39 passed；[记录](../logs/acp-code-unit-final-20261003.log) |
| Zed 实际配置命令 | Windows 和 WSL 配置中的命令均追加 `--help` 实际启动，退出码均为 0、stderr 为空，均显示三个沙箱选项；核验未改动设置，也未调用模型；[记录](../logs/zed-configured-launcher-check-20261003.json) |
| 最终静态检查 | 修改及新增 Python 的 Ruff 检查、56 个文件格式检查通过；全仓 Pyright 0 errors / 0 warnings；`git diff --check` 通过；66 个文件 UTF-8 与 Python SPDX 检查通过 |

后续“尽可能一致”的补充验收：

| 验证 | 结果与证据 |
| --- | --- |
| 共用配置与非法超时 | 47 passed；修改前已复现 Linux 接受非法时限和无穷时间配置，修改后统一拒绝；[记录](../logs/sandbox-portable-followup-20261003.xml) |
| Linux 超时、取消与原有沙箱回归 | 135 passed，含新增 4 项两端共用的原生超时/取消用例，以及原有 broker、executor、会话、描述符和序列化回归；[记录](../logs/code-timeout-parity-linux-final-20261003.xml) |
| Linux 直接创建进程限制 | 13 passed，包含新增 7 项和 exact-tool 回归 6 项；另有公开托管会话原生回归 2 passed；[进程与工具](../logs/linux-code-process-green-20261003.xml)、[公开会话](../logs/linux-managed-session-process-regression-20261003.xml) |
| Linux 宿主代理与提示 | 29 passed，包含真实宿主 PID、对象状态保留、数据快照和提示/配置回归；[记录](../logs/host-proxy-context-parity-wsl-20261003.xml) |
| Windows 超时配置合同 | 32 passed；[记录](../logs/windows-grace-policy-contract-20261003.xml) |
| Windows 超时、取消及原有生命周期回归 | 8 passed，254.38 秒，包含与 Linux 相同的 4 项新增原生用例和原有 exact-tool 回归；[记录](../logs/code-timeout-parity-windows-final-20261003.xml) |
| Windows 宿主代理实机 | 1 passed，71.75 秒；验证宿主 PID、对象状态、快照读取和重新赋值，未退回宿主执行生成代码；[记录](../logs/host-proxy-parity-windows-20261003.xml) |
| ACP 常用类型与服务端回归 | 10 passed；[记录](../logs/acp-namespace-parity-20261003.xml) |
| WSL 最终 ACP 协议 | 1 passed，30.87 秒；新增 `Path` / `Context` / 配置类型，连同真实文件、宿主 HTTP、取消、历史恢复与新一轮执行；[记录](../logs/acp-code-parity-followup-wsl-20261003.xml) |
| Windows 最终 ACP 相同协议 | 1 passed，191.60 秒；[记录](../logs/acp-code-parity-followup-windows-20261003.xml) |
| 补充静态检查 | 所有修改/新增 Python 的 Ruff 检查通过，66 个 Python 文件格式检查通过；全仓 Pyright 0 errors / 0 warnings；76 个源码/文档的 UTF-8 与 Python SPDX 检查通过；`git diff --check` 通过。 |

## 已应用的 Zed 配置

`C:\Users\QinGu\AppData\Roaming\Zed\settings.json` 已配置两个入口：

- `NOOA Luna (Windows, 代码沙箱)`：使用本项目 `.venv\Scripts\nooa-acp.exe`。
- `NOOA Luna (WSL, 代码沙箱)`：使用 Ubuntu 中的 Python 3.12 环境
  `/home/qinguang/.venvs/nooa`，工作目录为 `/mnt/e/rivon/labs-OO-Agents`。
  已核实 `nooa`、`nooa_acp`、`nooa_cli` 均从这份项目源码导入。

两者都指定 `--sandbox auto --sandbox-mode code --sandbox-network off`，
模型仍为 `local-gpt-5.6-luna`。保留原有其他设置和环境配置。
WSL 启动脚本 `nooa_wsl_launcher.py` 已支持并转发这些选项，既有路径和资源
URI 转换逻辑保持一致。

备份均位于 `C:\Users\QinGu\AppData\Roaming\Zed`：

- `settings.json.backup-parity-20261003-135038-130`
- `nooa_wsl_launcher.py.bak-sandbox-parity-20261003-134309`

使用时重启 Zed，再新建 Agent 会话并选择以上任一入口。旧会话不会自动切换
运行方式。本次完成原生进程、ACP 协议和启动配置核验，没有声称已在 Zed UI
中实测模型联网搜索；macOS 也未经实现或验收。
