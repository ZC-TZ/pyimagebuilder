# Dockerfile 语义支持范围

[文档目录](README.md) · [项目首页](../README.md)

本页对应纯 Python builder 当前实现。**支持 18 个 Dockerfile 指令名**；“支持”表示下表所列形式具有实际构建语义，不表示兼容 Docker/BuildKit 的全部写法。未知指令或不支持的 flag 会报错，不会静默跳过。先用 `python main.py build <context> --check` 做静态检查；这一步不拉取镜像，也不执行 `RUN`。

## 指令总览

| 指令 | 已实现的主要语义 | 需要注意 |
| --- | --- | --- |
| `FROM` | 选择基础镜像或 `scratch`；支持多个阶段、`AS` 和 `--platform` | 自动拉取需要完整镜像引用；平台限 `linux/amd64`、`linux/arm64` |
| `ARG` | 构建参数及默认值；支持 `FROM` 前全局 ARG、`--build-arg` 和父阶段参数继承 | 同一条 ARG 按声明顺序展开默认值。无默认值的再次声明优先使用 CLI 或全局值，否则保留当前/父阶段值；无关阶段不继承。阶段内使用全局 ARG 需要重新声明；不要传密码 |
| `RUN` | shell/JSON exec 形式，在基础镜像 rootfs 中真正执行并生成 layer | 必须加 CLI `--run`；需要 Linux/WSL、可用 namespace/OverlayFS 和执行权限 |
| `COPY` | 从本地 context 或已完成阶段复制文件；支持 heredoc | `--from` 不支持直接引用未作为构建阶段的外部镜像 |
| `ADD` | 本地文件/目录、本地 tar 展开、HTTP(S) 下载 | 不支持 Git 仓库；远程下载上限 4 GiB，`--offline` 时拒绝 |
| `WORKDIR` | 更新工作目录，按当前 USER 为缺失目录生成 layer | 已存在的目录保持原属主；相对路径基于当前工作目录。可用引号或转义空格指定一个路径，单引号内或转义的美元符保持字面量 |
| `ENV` | 设置最终镜像环境变量 | 使用 `KEY=value` 形式；不支持旧式 `ENV KEY value` |
| `USER` | 设置后续 `RUN` 与最终镜像的用户/组 | 接受 `user[:group]`；真实 `RUN` 仍受沙箱与基础镜像账户限制 |
| `SHELL` | 设置后续 shell form 的解释器 | 仅 JSON 数组形式 |
| `CMD` | 设置最终镜像启动命令 | 保留 shell form / JSON exec form 区别；构建时不执行 |
| `ENTRYPOINT` | 设置最终镜像入口命令 | 保留 shell/exec 区别；构建时不执行 |
| `EXPOSE` | 写入端口元数据 | 端口 1–65535，可带 `tcp`、`udp`、`sctp`；不实际开放端口 |
| `VOLUME` | 声明卷并创建必要目录 | 路径必须为容器内绝对路径；不挂载宿主目录 |
| `LABEL` | 写入镜像标签 | 使用 `KEY=value` 形式 |
| `HEALTHCHECK` | 写入健康检查配置；支持 `CMD` 和 `NONE` | 构建时不运行探针 |
| `STOPSIGNAL` | 写入停止信号 | 接受已支持的 Linux 信号名或 1–64 数字 |
| `ONBUILD` | 在基础镜像中保存触发器，派生镜像构建时执行 | 不允许嵌套 `ONBUILD` 或触发 `FROM`、`MAINTAINER` |
| `MAINTAINER` | 兼容旧 Dockerfile，写入 `author` | 建议新文件用 `LABEL` |

`COPY`/`ADD` 可用数字或基础镜像账户名指定 `--chown`，并可用八进制 `--chmod`。构建目录中的 `.dockerignore` 目前只允许空行和注释；含实际忽略规则时会明确失败，避免错误地把应忽略文件装进镜像。`RUN` 的执行条件与镜像归档的可导入性是两回事：无 `RUN` 的 Dockerfile 可在 Windows 原生打包；有 `RUN` 的构建必须具备 Linux 执行环境。

镜像层的普通成员不能同时包含非目录父路径及其子路径（例如同时有普通文件 `a` 与 `a/b`），不论 tar 成员先后顺序都明确拒绝；下层目录被上层文件替换仍允许。whiteout 是下层删除标记，单独处理，不作为新文件树成员参与这项检查。

本地源符号链接可作为链接节点复制，但不允许穿过源的父目录链接读取目标文件。Windows junction 与普通目录的语义不同，构建、缓存输入扫描和严格构建快照均明确拒绝；目录树内已被 `--exclude` 排除的 junction 不会被读取。递归 `**` 不跟随目录符号链接。源通配符只解释源表达式，上下文根目录名中的 `[1]` 等字符按字面量处理。静态 `build --check` 共用源通配符和路径检查，但仍不等于实际构建及完整目录树验收。

省略 `--chown` 的组时，数字 UID 使用同值 GID，用户名使用镜像内 `/etc/passwd` 的主组；显式组名查镜像内 `/etc/group`。WORKDIR 新目录使用相同的属主解析规则；数字 `USER 1000` 的目录为 1000:1000，`USER app` 的目录为 app 的 UID 和主组。COPY/ADD 的属主不会自动跟随 USER。缺失或无法解析的具名账户会报错；当前不支持 `--chown=app:` 这种省略组但保留冒号的写法。

COPY/ADD 自动创建的目标目录和中间父目录同样采用 `--chown`、`--chmod`；未指定时为 0:0、0755。已有父目录保留原有属主和权限。此规则也适用于 heredoc、远程 ADD、归档解包以及多阶段 COPY；源目录中实际复制的子目录仍按源元数据和传输选项处理。指令缓存版本为 9，旧版本目录元数据可能有误，首次使用会自动重新构建相应层。

## 现代语法与 flag 细节

| 语法 | 当前行为 |
| --- | --- |
| `HEALTHCHECK CMD` / `NONE` | 支持 shell、JSON exec、`interval`、`timeout`、`start-period`、`start-interval`、`retries`；写入 `config.Healthcheck`，时长为纳秒。构建时不执行探针。 |
| `STOPSIGNAL` | 支持 Linux 信号名或 1–64 数字，写入 `config.StopSignal`。先按原大小写展开 ARG/ENV 名称，再规范化信号名；字面量 `$name` 不是合法信号。 |
| `RUN <<EOF` / `COPY <<EOF` | 支持 `<<-` 去 tab、引用分隔符抑制 COPY 变量展开；RUN 默认 shell、常见 shell/Python 解释器与 shebang。含多个 heredoc 或 shell 重定向的 RUN 会交由镜像内 shell 解释。COPY 只接受单个 heredoc。 |
| `COPY --exclude` / `ADD --exclude` | 支持重复标志，作用于本地目录树、stage 文件和 tar 成员。使用 Python glob/fnmatch，少数 Go `filepath.Match` 边界模式可能不同。 |
| `COPY --link` / `ADD --link` | 独立生成 layer；它的层缓存键不包含先前层状态。具名 `--chown` 与 `--link` 的组合拒绝；请使用数字 UID:GID。仍需读取基础镜像来生成最终 tar，没有 BuildKit 的远端零下载 rebase。 |
| `COPY --parents` | 支持保留源路径及 `/./` 分界，支持本地与已完成 stage 的源。分界之前的目录不进入目标路径，`.hidden` 等隐藏目录名保持完整。 |
| `ADD --unpack=true/false` | 本地 tar 默认展开；远程 tar 默认保留文件。显式标志覆盖默认值。当前 zstd tar 展开会明确报错。 |
| `ADD --checksum=sha256:...` | 对 HTTP(S) 下载按原始字节验证；有 checksum 时可安全命中层缓存，无 checksum 时每次下载并绕过层缓存。`--offline` 和严格 hermetic 构建拒绝远程 ADD。 |
| `RUN --network=default/none/host` | 每条 RUN 可覆盖网络；`host` 还要求 CLI `--network=host`。`default` 采用本工具 CLI 的默认网络策略。 |
| `RUN --security=sandbox/insecure` | `sandbox` 使用 CLI 选择的隔离级别；`insecure` 只在显式 `--run-sandbox=legacy` 时允许。legacy 不等同于 Docker BuildKit 的完整 privileged entitlement。 |
| `FROM --platform`、平台 ARG | 提供 `TARGETPLATFORM/TARGETOS/TARGETARCH/TARGETVARIANT` 和 `BUILDPLATFORM/BUILDOS/BUILDARCH/BUILDVARIANT`；按 Docker 的作用域规则，stage 内需再次 `ARG` 声明。仅输出 linux/amd64、linux/arm64。 |
| `ONBUILD` | 写入基础镜像触发器；作为后续构建的 `FROM` 时在该构建的 context 中执行，执行后从子镜像配置中移除。基础配置的 `OnBuild` 缺省、null 或空数组均表示没有触发器，也允许随后新增 ONBUILD。外部触发器必须恰好包含一条指令，允许该指令含 heredoc；拒绝空内容或多条指令。触发器作为 `ONBUILD` 阶段显示，不占用子 Dockerfile 的 `[n/N]` 指令号。禁止嵌套 ONBUILD、FROM、MAINTAINER。 |
| `# syntax` / `# escape` / `# check` | 接受 `docker/dockerfile` 家族的 syntax 标识，但不会下载或执行外部 frontend；escape 支持反斜杠和反引号续行；check 只接受 `skip=...`，其余要求显式报错，因为本工具没有实现 BuildKit build checks。 |
| 变量展开 | 支持 `$VAR`、`${VAR}`、默认值和替代值修饰符、前后缀 glob 删除、首次或全部匹配替换及 `\$` 转义。COPY/ADD、ENV、LABEL、ARG、WORKDIR 等构建期指令在词解码时保留单引号及转义中的字面量美元符；JSON 形式保留 JSON 字符串解码规则。RUN/CMD/ENTRYPOINT 的 shell form 仍由容器内 shell 展开。 |
| shell / JSON exec | RUN、CMD、ENTRYPOINT、HEALTHCHECK 分别保留 shell/exec 语义；SHELL 会改变后续 shell form；新 ENTRYPOINT 清除未在当前 stage 明确设置的继承 CMD。 |
| `MAINTAINER` | 兼容解析并写入旧式 config `author` 字段。 |

**明确未实现：**Git 仓库 `ADD`、任意 Dockerfile frontend 插件、BuildKit 全部 build checks、完整 BuildKit `security.insecure` entitlement。远程 HTTP(S) ADD 限制 4 GiB，且不会使用 Docker 的 build context Git clone 语义。对这些写法会报错，不会静默当作其他指令执行。

标准参考：[Dockerfile reference](https://docs.docker.com/reference/dockerfile/)。
