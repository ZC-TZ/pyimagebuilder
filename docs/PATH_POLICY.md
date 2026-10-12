# 路径处理规则

[返回文档目录](README.md)

路径不能只检查输入字符串。应先确定路径属于哪个命名空间，再按消费端的规则解析，最后检查根目录、重叠、链接和重复名字。共用规则位于 `path_policy.py`，回归入口为 `tests/test_path_policy.py`。

## 四类路径的区别

| 类型 | 示例 | 处理规则 |
| --- | --- | --- |
| 镜像内 Linux 路径 | `/app`、`../app`、`//app` | 按 WORKDIR 解析；统一前导斜杠和 `.`/`..`，再检查操作边界 |
| tar / ZIP 相对成员名 | `./app/file`、`app//file` | 拒绝绝对路径、任何 `..` 组件、NUL 和反斜杠；等价名字用于重复检测 |
| 宿主文件路径 | `D:\packages\app.war` | 显式输入可解析链接；新交付输出拒绝最终节点链接，发布时排他创建 |
| URL 路径 | Registry 上传 Location、制品目录 href | 单独检查编码、路径组件和仓库边界；不能拿宿主 Path 代替 URL 校验 |

反斜杠在 Linux 文件名中可以是普通字符，但当前构建器不支持此类镜像路径、归档成员和符号链接目标，因此明确拒绝，不能悄悄将其转换成 `/`。普通 Linux 文件名中的冒号仍可在虚拟 rootfs 索引和归档中保留。

## 镜像路径：规范化不等于允许操作

`//proc` 统一为 `/proc`，`/cache/..` 统一为 `/`。随后由操作本身决定是否接受：

- WORKDIR、COPY 到根目录等普通 Dockerfile 操作可以使用 `/`。
- RUN cache/secret mount 不允许覆盖根目录、`/proc`、`/sys`、`/dev` 及其后代；所有挂载目标按规范化结果检查重复和父子重叠。
- Fast 的 deployDir 必须是绝对非根目录，不接受父跳转、换行或可能被 Dockerfile 展开的特殊字符。自动生成的 COPY 使用 JSON 形式，支持目录中的空格。
- 符号链接目标保留原始 Linux 链接语义。相对 `..` 按组件解析，不用字符串前缀推断是否越界；绝对链接解释为镜像内路径。

## 归档与 Windows 解压

`app/file`、`app//file`、`app/./file` 是同一个规范名字；同一归档出现这些别名时按重复成员拒绝。根目录头只能是目录。硬链接目标在应用 whiteout 或写文件前检查，避免先修改文件系统再发现无效路径。

ZIP 解压及 Windows rootfs 物化还会预检宿主文件名：

- 拒绝嵌套盘符、ADS（如 `file:stream`）、设备名（如 `NUL.txt`）及尾部点/空格。
- 拒绝 Windows 文件名非法字符和控制字符。
- 整批检查大小写别名，包含隐式父目录；例如 `Assets/a` 与 `assets/b` 不能在 Windows 上悄悄合并。

这些 Windows 限制只用于实际宿主路径，不强加给纯逻辑 Linux 镜像索引。真实 RUN 仍只支持 Linux / WSL。

## 链接、缓存和输出

| 消费端 | 当前处理 |
| --- | --- |
| COPY 构建上下文 | 可复制符号链接自身；拒绝通过目录链接/junction 读取上下文外文件 |
| provenance 上下文摘要 | 普通符号链接记录目标文本，不遍历；遇到 junction 明确拒绝 |
| Artifactory 制品打包 | 只收普通文件，遇到文件链接或目录链接拒绝；下载入口也检查成员名与落盘路径 |
| `system df` | 不遍历目录链接或 junction，不统计库外内容 |
| rootfs 物化和删除 | 绝对符号链接限制在镜像根内；不通过 Windows junction 删除宿主目标 |
| 指令缓存 / 基础层快速缓存 | 键不能作为相对路径；内部目录、条目和层不能通过链接越界复用 |
| CAS | 内部目录、ref、blob 检查链接；引用文件不能作为库外 JSON 的入口 |
| build / pull / save / tag / export / import / flatten 等输出 | 最终节点链接（包括断链）必须在 resolve 前拒绝；最终发布仍使用排他操作 |
| OverlayFS | lowerdir / upperdir / workdir 路径不允许逗号、冒号、反斜杠或换行等选项分隔字符 |

用户显式指定的输入文件或数据根目录可以位于解析后的宿主位置；这不代表允许镜像成员、缓存条目或 ZIP 文件自行跳转到任意宿主位置。

指令缓存版本为 **18**，旧规则生成的指令层自动失效。CAS schema 仍为 1，基础层缓存版本仍为 2，正常镜像 blob 不需要重新下载。权限处理详见 [镜像权限规则](PERMISSION_POLICY.md)。

## 测试与边界

专项测试覆盖路径别名、根与虚拟挂载边界、归档重复、Windows 组件和大小写冲突、junction、缓存键、URL 编码、输出链接、Fast 含空格目录及实际 COPY/WORKDIR 构建结果。Windows junction 使用真实临时目录；文件 symlink 权限不足时，部分测试模拟 lstat 判断，并明确注释。

这些检查不构成对恶意宿主并发替换父目录的全面防护。构建上下文、缓存与私有工作目录应由当前用户控制并保持稳定；预检不能代替最终发布的排他创建，也不能代替真实 Linux namespace、OverlayFS 和 Docker 的目标环境验收。
