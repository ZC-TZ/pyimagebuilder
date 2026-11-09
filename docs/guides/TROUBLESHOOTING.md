# 常见问题

[文档目录](../README.md) · [统一配置](CONFIGURATION.md) · [性能排查](PERFORMANCE.md)

先保留报错和本次日志，再按现象定位。构建可加 `--debug` 获取 traceback；CLI 逐行日志使用 `--progress plain`。其他命令的可用开关以自身 `--help` 为准。

## 根据现象处理

| 现象 | 先核对什么 | 下一步 |
| --- | --- | --- |
| Fast profile already exists | 已有同名模板 | 编辑原模板，或使用另一名称；init 不用于反复覆盖 |
| Unknown fast profile / 配置找不到 | 配置查找顺序、模板名 | 显式指定 --config 与 --profile |
| 基础 tar 不包含 FROM 标签 | baseImage 与归档内标签 | inspect 查看真实标签并对齐 |
| 找不到 FROM / 离线构建失败 | 镜像库、平台、来源、本地输入 | 在同一配置下 pull/load，或提供基础 tar/map |
| 下载 401/403 | 账号、passwordEnv、启动环境 | 重新设置变量并确认仓库读取权限 |
| TLS 证书校验失败 | 完整协议、CA、主机名与有效期 | 按下方 TLS 说明处理 |
| COPY --chown 无法解析账户 | 基础镜像 passwd/group | 使用实际账户或适用的数字 UID:GID |
| .dockerignore 报不支持 | 存在实际忽略规则 | 整理专用 context，按兼容表使用 --exclude |
| Output already exists | 同名输出已存在 | 换用新文件名，核对旧结果后再自行处理 |
| Image reference is busy | 同一引用正在操作 | 等操作结束后重试，不删除 lock 文件 |
| 暂存文件已过期或已提交 | 上传令牌是否仍有效 | 重新选择文件；复用任务时也需重新选暂存输入 |
| 暂存文件/目录已替换，或清理失败 | 文件被移走、替换、占用或目录出现额外文件 | 核对暂存位置与权限；工具保留替换节点，不能靠改路径绕过 |
| Start Python with PYTHONHASHSEED=0 | 封闭模式的启动环境 | 启动 Python 前设置哈希种子，再生成锁/构建 |
| RUN 在 Windows 原生失败 | 没有 Linux 内核执行环境 | 用 CLI 的 WSL 方式，并验收 namespace/OverlayFS |
| arm64 RUN 出现执行格式错误 | 宿主与基础镜像架构 | 配置 QEMU/binfmt_misc，或改用同架构 Linux |
| docker load 不接受 rootfs.tar | 它只是文件系统归档 | import 生成镜像；保留原配置时选 flatten |
| history 显示未记录 | 原镜像没有完整 history | 不据此判定层损坏，另用 verify 检查 |

## 浏览器能打开，但仓库诊断提示 TLS 证书校验失败

复制浏览器中的完整地址，确认是 `http://` 还是 `https://`。“不安全”图标不能区分 HTTP 与证书无效的 HTTPS，浏览器证书例外也不会传给 Python。

| 实际连接 | 配置要点 |
| --- | --- |
| HTTP 服务 | 对应仓库项设 `insecureHttp: true` |
| HTTPS，正常校验 | `insecureHttp: false`，使用系统信任或配置 `caFile` |
| HTTPS，需要兼容无效证书 | `insecureHttp: false, verifyTls: false`，只对该仓库来源生效 |

这些是 JSON 字段说明，实际值用布尔量 `true/false`，不是字符串。`insecureHttp` 选择协议，`verifyTls` 控制 HTTPS 证书校验，两个含义不同。

保留已有账号与密码变量名，编辑配置后重启 Web，再做仓库诊断。程序不会因证书失败自动降级 HTTP；具体规则见[仓库连接配置](CONFIGURATION.md#3-下载地址与鉴权)。

## Invalid layer permission mode: 16877

`16877` 即八进制 `040755`，含目录类型位和 `0755` 权限。当前版本可处理与 tar 类型一致的这些类型位，同时保留特殊权限位；冲突、未知高位及非法值仍拒绝。

若旧代码在 FROM 阶段报此错误，更新代码、保留原配置与镜像库后重试。无需把权限改成 777 或重新下载正常基础镜像。规则见[镜像权限](../PERMISSION_POLICY.md)。

## 为什么改了配置却没生效

当前目录可能有另一份 config.json，或 `PYIMAGEBUILDER_CONFIG` 指向其他位置。显式指定 `--config`，核对网页显示的配置位置。配置里的相对路径以配置文件目录为基准。

`defaultProfile` 不覆盖显式 `--profile tomcat`。手工修改配置或密码环境后需重启工作台；网页保存的 Fast 模板可直接生效。

## 下载后的基础镜像放在哪里

使用 `cache.imageStore` 或命令支持的 `--image-store`。有配置但未指定时默认在配置目录的 `data/image-store/`；无配置时用系统临时目录中的默认镜像库。

普通 build/Fast 可直接消费 CAS；压缩层仍需解码、DiffID 与结构校验。压缩 blob、解码层及按需 tar 可能同时存在，磁盘占用可高于下载量。见 [CAS 镜像库](../CAS_STORE.md)。

## 为什么已经构建成功，images 里没有它

build/fast 输出归档并登记台账，不自动注册本工具镜像库。执行 `python3 main.py load -i app.tar` 后可按标签查库。Windows 使用 `python`。

`builds` 查成功历史，`images` 查本工具镜像库，都不会查询目标 Docker daemon。

## 为什么缓存命中了还会耗时

命中仍需检查输入、读取基础层、写出与校验完整产物。`--no-cache` 不刷新 FROM，`--pull` 不关闭指令缓存，`--verify-cache` 增加完整校验。

下载结束后仍可能在准备层或入库；层编号也不是完成数量。按[性能排查](PERFORMANCE.md)分开比较首次下载、本地复用和应用层命中，不要先清空镜像库。

## 跨盘导出为什么慢

导出包括写出、校验、发布和最终 SHA-256。跨盘复制可能增加一次整包读写；输出盘暂存优化是否可用，取决于硬链接、路径与工作区条件。

可用 `--workspace` 固定临时盘，并分开观察发布与最终摘要耗时。操作方法见[存储与工作目录](PERFORMANCE.md#5-存储与工作目录)，具体旧基准不能代替本机测量。

## Web 日志和历史为什么不完整

当前会话只保留最近 250 条日志，“导出当前日志”也只导出保留部分。任务摘要在重启后仍可读，旧原始日志不会从摘要恢复；CLI 完整日志保存见[性能指南](PERFORMANCE.md#2-怎样看日志)。

包版本查[打包台账](HISTORY.md)，运行失败看任务结果与日志。台账中的旧路径不会随着文件移动自动更新。

## 日志为什么出现反斜杠转义

普通终端只转义当前编码无法表示的字符；UTF-8 终端正常显示中文与 emoji。JSON 事件使用 ASCII 转义，解析后的字段值不变；这不表示文件路径、镜像 config 或摘要被改写。

阶段、日志行号、并行耗时解释见[进度输出参考](../PROGRESS.md)。

## 怎么安全清理

使用同一配置或相同数据路径。实际清理前停止共享目录上的构建、拉取、导入和其他写入。

1. `system df` 查看占用。
2. `cache prune --older-than 30 --dry-run` 预览指令缓存。
3. `image prune --older-than 30 --dry-run` 预览旧镜像；它可能包括已打标签的镜像。
4. `cas prune --dry-run` 预览无引用 blob。
5. 核对候选，再运行去掉 `--dry-run` 的对应命令。

`rmi` 删除引用不等于回收全部 blob；交付 tar 和台账也不是同一份数据。Web 只执行其界面明确支持并经过确认的清理。完整边界见[缓存规则](../CACHE_POLICY.md)。

## 扁平化、压缩和改标签会不会改变镜像 ID

转存和改标签应保留原始 config 字节及 ImageID。真正构建、rootfs import 和 flatten 生成派生镜像时，ImageID 变化是预期行为。

manifest digest、层 DiffID 与整个 tar 的摘要是不同概念。flatten 不保证体积变小，optimize 只保守删除冗余整层。详见[镜像身份](../IMAGE_IDENTITY.md)与[rootfs 指南](../ROOTFS_COMMANDS.md)。

## 支持 Python 3.7 是否已实测

Windows 上已分别用 Python 3.7.9、3.12 运行完整回归；最近结果与验收范围见[开发与维护](../../CONTRIBUTING.md)。这不能代替 UOS、真实 Linux RUN、WSL、内网鉴权及最终应用验收。

## 特殊路径为何被拒绝

敏感 RUN 挂载和 Fast 部署目录不允许解析到根目录；归档路径不允许父跳转或反斜杠。Windows 解压还会拒绝盘符、ADS、设备名、尾部点/空格和大小写冲突。按[路径规则](../PATH_POLICY.md)检查输入，保留边界校验。
