# OpenShell 与 ACP 原生沙箱：Windows 使用场景比较

核查日期：2026-10-03（Asia/Shanghai）。OpenShell 来源固定为官方 `main` 提交 [`48d9ab3d0d9a343365dea1b0cd87565050ac658e`](https://github.com/NVIDIA/OpenShell/commit/48d9ab3d0d9a343365dea1b0cd87565050ac658e)，提交时间 **2026-10-02 22:10:01 UTC**；通过 `git ls-remote` 和 `git show` 核实。以下描述的是该源码快照，不等同于确认所有功能已经进入某个稳定发布包。ACP 依据当前工作区 [acp-sandbox.md](acp-sandbox.md)，含尚未提交的实现文档。本次只阅读官方资料与源码，没有安装或运行 OpenShell，也没有做性能或对抗测试。

**建议：保留 ACP 的 LPAC + Job Object 后端；是否增加 OpenShell，取决于是否需要完整、持久、可联网的 Linux 编程环境。** 当前“有限文件工具 + 短命令 + 无互联网”的任务适合原生后端；若目标是让代理安装依赖、执行会保留产物的测试/构建、受控访问 API，则 OpenShell 更接近所需的运行环境。用户已有 WSL，因此可以沿现有 WSL 评估，但 Docker/内核兼容性和 ACP 适配仍需单独验证。二者的工作范围不同，不能仅凭“使用容器”或项目规模判定谁全面更安全。

## 平台和部署事实

- 官方支持矩阵将 Linux x86_64/arm64、Apple Silicon macOS 列为支持；Windows 路线是 **WSL 2 + Docker Desktop、x86_64、Experimental**。工作负载是 Linux OCI 镜像，发布的可信 `openshell-sandbox` 二进制也是 Linux。没有必要为了它购买 Mac。[1]
- 当前源码已有 **Windows MXC driver**，因此不能说“没有任何原生 Windows 代码”；但官方运行时表将它列为 **Coming soon**，不能当作当前正式支持的 Windows 后端，更不能把 Linux supervisor 的全部保证直接套给 MXC。[2][3]
- 当前 OpenShell 已支持 Docker、Podman、Kubernetes、MicroVM，并非只能部署 Kubernetes。Docker 路线要求 Docker 28.0+；文档还要求 Docker Desktop 开启 host networking、不能启用 Enhanced Container Isolation。MicroVM 使用 Linux KVM 或 macOS Hypervisor.framework。[1][2]
- Linux 工作负载边界要求 Landlock ABI 3（上游 Linux 6.2+，发行版回移植仍须通过探测）、seccomp 通知等设施。不能仅因为安装了 WSL/Docker 就断言兼容。必需能力探测失败会拒绝启动；用户文件策略的 `best_effort` 是另一个层次，默认允许兼容降级，但不能关闭保护私有通信目录的强制 Landlock 基线。[1][9]

## 对当前项目的实际差别

| 方面 | 当前 ACP 原生沙箱 | OpenShell 官方 Linux 运行时 |
|---|---|---|
| 原生 Windows | 已有 LPAC 进程权限和 Job Object 资源/后代进程约束 | 已公布的 Windows 支持路线仍依赖 WSL 2 + Docker Desktop；MXC 尚标为 Coming soon |
| 隔离对象 | ACP 中生成 Python 和专用命令执行器；服务端与模型客户端在信任边界内 | 完整 Linux 工作负载，另有 gateway、可信 supervisor、计算运行时 |
| 命令产生的修改 | 在临时工作区快照里执行，命令修改丢弃；持久修改走受限文件工具 | 有可持续使用的工作区；stop/start 保留工作区数据，具体持久性跟随运行时存储语义 |
| 依赖 | 不自动安装；快照排除 `.venv`、`node_modules` 等 | 可预装到镜像；也可在允许写入的路径安装，并为包源和对应二进制配置联网策略 |
| 网络 | 当前 ACP profile 不授予互联网 | 默认拒绝出站；按二进制、目标端点及可选 HTTP 请求规则授权 |
| 凭据 | 命令不接收模型凭据；Linux fork Python 明确不保证宿主内存中凭据保密 | provider 密钥保存在工作负载外，代理只拿占位符，supervisor 为获准请求替换 |
| 维护成本 | 维护本项目的 Windows/Linux 原生安全边界 | 维护镜像、gateway、运行时、策略、provider、文件同步和版本兼容 |

ACP 列依据本仓库 [ACP 原生沙箱说明](acp-sandbox.md)；OpenShell 列依据架构、运行时、生命周期、provider 与默认策略文档。[1][2][4][5][6][9] 官方 PyPI profile 明确支持 Python/uv 包源访问，但要求操作者按镜像中的真实可执行文件路径修改并导入，绝非默认开放全部网络。[10]

## OpenShell 的隔离增益及边界

**官方描述的机制：** 当前架构将 supervisor 与不可信工作负载分开。Docker/Podman 的工作负载容器关闭网络，只经认证 Unix socket 与独立 supervisor 容器通信；Kubernetes 使用独立 supervisor pod 和 NetworkPolicy；MicroVM 无网络设备，经 vsock 通信。工作负载采用非 root 身份、无 Linux capabilities，Landlock 限制文件，seccomp 通知拦截 TCP/DNS；supervisor 作策略决定并建立实际外部连接。[4]

**凭据保证有条件：** OpenShell 管理的 provider 密钥不会直接进入代理环境；代理获得 opaque placeholder。替换要求“二进制/目标的网络策略”与“provider 的 host/port/path 绑定”都通过，而且流量必须由代理按 HTTP 检查。`tls: skip` 或非 HTTP 原始隧道不支持凭据替换。它不会自动移除用户自己放进镜像、文件、普通环境变量或挂载目录的真实密钥；后一句是从保证的作用范围得出的工程推论，不是额外承诺。可信 gateway、supervisor、运行时和宿主仍需要受信任。[4][6]

**不能推出的结论：** 开放一个合法 API 端点仍然可能允许代理向它发送工作区内容；允许联网意味着需要审查策略。绑定挂载会把宿主文件暴露给工作负载，官方特别警告其可绕过工作区隔离。Kubernetes CNI 必须实际执行 NetworkPolicy。新 policy prover 只验证其模型覆盖的策略属性，官方明确说通过检查不代表策略适合某项任务，也不代表运行中的沙箱已实际执行该策略。因此“有形式化验证”不等于整个沙箱不存在漏洞。[2][7]

这些机制能解决更完整的代理运行环境问题，但没有本次实测可支持“比 LPAC 更安全/更快”的总排名。尤其当前 ACP 默认无网络，若迁入 OpenShell 后开放大量端点，不能只因多了一层容器就认为风险更低。

## ACP 集成判断（工程建议，未实现/未验证）

1. **保留现有 Windows backend。** 当前“有限文件工具 + 短命令 + 无互联网”的需求与原生实现吻合。
2. **需要 Linux 完整环境时，再新增可选执行后端。** OpenShell Python SDK 已提供创建、等待就绪、执行、会话复用、停止/启动和删除；可据此适配 ACP，但官方这些能力不等于已经提供兼容本项目的 ACP 适配器。[8]
3. 适配前必须确定 Windows 与 Linux 路径、工作区上传/下载和冲突、命令改动回写、stdout/stdin 协议、取消和资源回收、恢复会话、依赖镜像等语义。OpenShell 的持久工作区不能直接替换当前“命令快照执行后丢弃”的约定。若只把 `run_command` 放入 OpenShell，而生成 Python 仍保留原来的 Linux fork worker，其继承宿主内存的问题仍然存在；要改变那条凭据边界，必须连同相关生成代码执行路径一起隔离。[5][8] 最后两句为基于现有 ACP 结构的推论。

当前 README 已进入 0.1.x；官方对稳定发布提供兼容性和维护策略，但 Windows/WSL 仍列 Experimental，实验接口可以在补丁版改变。选用时应固定相同系列的 CLI/SDK/gateway/runtime，并用自己的 ACP 工作负载验收，不宜把本次 `main` 调研视作生产资格认证。[1]

## 官方来源

全部 OpenShell 链接固定到本次提交，避免 `main` 后续变化使结论失去上下文。

1. [Support Matrix：平台、版本策略、内核与软件要求](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/docs/about/support-matrix.mdx)
2. [Sandbox Runtimes：运行时、Windows MXC 状态、存储与部署限制](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/docs/how-it-works/sandboxes/runtimes.mdx)
3. [Windows MXC driver 源码入口](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/crates/openshell-driver-mxc/src/lib.rs)
4. [Architecture：工作负载、supervisor 与信任边界](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/docs/about/architecture.mdx)
5. [Sandboxes：命令、文件传输、持久工作区与生命周期](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/docs/how-it-works/sandboxes/overview.mdx)
6. [Providers：占位符、端点绑定和凭据注入限制](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/docs/how-it-works/providers/overview.mdx)
7. [Policy Prover：验证模型与保证的范围](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/docs/how-it-works/policies/prover.mdx)
8. [Python SDK：gateway 连接、exec 与会话接口](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/docs/sdk/python.mdx)
9. [Default Policy：文件权限、best_effort 与强制基线](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/docs/how-it-works/policies/default-policy.mdx)
10. [官方 PyPI provider 示例](https://github.com/NVIDIA/OpenShell/blob/48d9ab3d0d9a343365dea1b0cd87565050ac658e/providers/pypi.yaml)
