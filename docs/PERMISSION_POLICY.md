# 镜像权限处理规则

[返回文档目录](README.md)

权限属于镜像文件系统内容。文件字节相同，并不代表 UID/GID、mode、扩展属性或硬链接关系相同。`permissions.py` 统一验证层权限声明，并合并同一节点应保留的扩展属性；消费者不能根据最后一个原始 tar 头推断最终权限。

## 1. 从输入到输出

| 流程 | 权限规则 |
| --- | --- |
| Registry / Artifactory 下载、CAS、原样转存 | 校验并保留原始 blob；宿主存储文件的权限不替代层内权限 |
| 本地 COPY | 文件及被复制子目录的 mode 来自上下文；属主默认 0:0，支持 `--chown` / 八进制 `--chmod`；不读取宿主 xattr |
| 跨阶段 COPY | 使用源虚拟文件树的有效 mode、UID/GID 和 SCHILY.xattr；传输参数覆盖相应权限 |
| ADD 本地 tar | 保留成员声明的权限和 SCHILY.xattr；传输参数可以覆盖属主与 mode；编码前检查有效权限声明 |
| 自动补建父目录 | 默认 0:0、0755；COPY/ADD 的 `--chown` / `--chmod` 同样用于新父目录；已有父目录保持原权限 |
| WORKDIR / VOLUME | 按各自指令规则补建目录；不是对已有目录批量执行 chown/chmod |
| export / flatten | 输出最终可见节点的有效属性，保留根目录头及硬链接 inode 分组 |
| 优化器 | 比较有效 mode、属主、时间与 xattr，不能删除仍提供权限信息的层；目前仍拒绝硬链接和特殊文件层的裁剪 |

符号链接不执行 chmod，避免意外修改目标。硬链接共享 inode：后续链接头的权限更新要体现在所有仍可见的别名上；删除或覆盖一个名字不代表其他名字也被替换。

## 2. 目录更新与扩展属性

上层目录头更新已有目录时，采用新的 mode、UID/GID、时间，并保留未被新头覆盖的 SCHILY.xattr。根目录采用同一规则。下层目录被 whiteout 删除后重新创建、或被其他节点类型替换，不再继承已删除节点的属性。

opaque 只遮蔽下层子内容，不清除目录自身的权限和 xattr。普通文件被重新创建时，也不能继承被替换文件的属性。

这与 [Moby 层解包实现](https://raw.githubusercontent.com/moby/go-archive/main/archive.go)复用已有目录并设置声明 xattr 的行为相对应。归档转存能保留 tarfile 可无损表示的二进制 xattr；当前 RUN 物化仅支持 UTF-8 xattr，非 UTF-8 值在整层预检时明确失败。ACL、capability 和其他属性是否能落盘，还取决于宿主文件系统及执行权限，不能静默忽略设置错误。

**当前 RUN 不支持删除已有目录或根目录的 xattr。** 目录头中缺少某个属性，不会让 Docker 解包器删除它；空值也不等于删除。仅输出 opaque 会改变子内容可见性，却不能解决属性删除，因此这类 RUN 结果明确失败，不发布镜像或写入指令缓存。普通文件替换后的属性删除，以及目录属性新增或更新仍可处理。

## 3. RUN 权限准备

物化器与 OverlayFS 根目录恢复均按 **chown → xattr → chmod** 处理，避免 chown 清除 setuid/setgid、或 ACL 写入改变最终 mode。mtime 在物化器最后恢复；符号链接只设置适用的属性。

RUN 的执行子进程默认 umask 为 **0022**，与 [runc 初始化规则](https://github.com/opencontainers/runc/blob/main/libcontainer/SPEC.md)对应；不改变构建器父进程的 umask。RUN 命令可以显式设置自己的 umask。

| 自动创建对象 | 权限 |
| --- | --- |
| 镜像内挂载父目录、cache 挂载点、proc/dev 目录 | 0:0、0755；已有目录不重置 |
| 新 `/tmp` | 0:0、1777；已有 `/tmp` 保持原属性 |
| 新持久 cache 来源目录 | 0755，属主为执行构建的宿主身份；rootless 中映射为 0:0；已有 cache 保持 RUN 留下的属性 |
| root 模式的 `/dev/null` 等设备节点 | 创建后显式恢复 0:0、0666，排除宿主 umask 的影响 |

rootless 仍使用单 UID/GID 映射，只支持镜像 0:0 及受支持的节点类型，不保证任意镜像均可物化。secret 只读 bind mount 保留显式来源文件的权限；工具不会为了让任意 USER 可读而放宽宿主秘密文件。

### OverlayFS 内部属性

按照 [Linux OverlayFS 规则](https://docs.kernel.org/filesystems/overlayfs.html#user-xattr)，root 模式只识别 `trusted.overlay.*`，带 `userxattr` 的 rootless 模式只识别 `user.overlay.*`。另一 namespace 的属性按普通 xattr 保留，不能回退查找并误作 whiteout/opaque，也不能无条件丢弃。

xattr whiteout 必须是零长度普通文件；非空文件或目录带此标记时明确拒绝导出，不能丢弃其内容并生成删除层。基础镜像中若声明本次 RUN 挂载使用的保留 xattr namespace，物化整层前报错，避免普通 PAX 属性被内核当作删除、opaque 或 metacopy 控制信息。纯归档转存仍保留原始 blob；该限制属于 RUN 执行路径。

## 4. Fast 的 WAR 准备

硬链接与复制回退应提供相同的 WAR 文件 mode。跨盘或不支持硬链接时，复制完成后恢复源 mode。Windows 只读 WAR 使用独立副本，构建成功或失败后仅恢复副本的删除权限，兼容 Python 3.7；不通过 chmod 硬链接改变原 WAR。

Fast 的 dist.zip 当前不按 ZIP 的 Unix 外部权限字段恢复 mode，解包后的上下文权限仍由宿主文件系统表示。需要严格指定 Unix 权限时，使用普通 Dockerfile 配合 `COPY --chmod`；不要将此流程视为跨操作系统保留所有 ZIP 权限的工具。

## 5. 验证、缓存与边界

- 有效层 UID/GID 范围为 0..4294967294；拒绝 chown 哨兵值。mode 只接受 0000..7777 的 Unix 权限位，包含 setuid/setgid/sticky。
- 在应用 whiteout、改变索引或物化整层前检查权限声明；生产层也在 tar 编码前检查，避免 tarfile 静默截掉非法 mode 高位。
- 指令缓存版本为 **22**，旧版本的构建结果失效；CAS schema 仍为 1，基础层缓存版本为 3，不要求重下载合法基础镜像 blob。
- `tests/test_permission_contract.py` 覆盖目录继承、opaque、重建、COPY、CAS/cache、双格式 flatten、优化与系统调用顺序；其他权限测试覆盖 inode、root、USER、链接和自动目录。
- `tests/test_overlay_xattrs.py` 覆盖实际挂载 namespace 的标记识别、非活动 namespace 属性保留、非法 whiteout、目录 xattr 删除拒绝，以及构建失败不发布或缓存。
- `tests/test_layer_acceptance_cache.py` 覆盖无效链接/属主、RUN 写盘失败不发布该指令缓存，以及修复执行条件后重试、有效命中和先前成功指令缓存保留。
- `tests/test_copy_inode_groups.py` 覆盖 COPY 目录内部 inode 分组、独立源复制、排除/删除后的别名、覆盖导致的 inode 分离，以及 CAS、多阶段、缓存和双格式归档。
- `tests/linux_phase13_smoke.py` 在真实 Linux 上以宿主 umask=077 验证 RUN 新文件与 cache 来源目录的权限，可通过 `conformance.py linux` 执行。

Windows 上的系统调用模拟不能证明 Linux ACL、capability、user namespace、OverlayFS 或设备节点已通过内核验收。Windows chmod 也不等同于 Unix mode 或 Windows ACL 管理。真实 Linux/WSL 及 Docker 对照须在目标环境运行；输入、缓存和私有工作目录应由构建用户控制并保持稳定。
