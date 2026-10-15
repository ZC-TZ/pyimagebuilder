# 并发与资源生命周期

[文档目录](README.md) · [架构说明](ARCHITECTURE.md) · [缓存规则](CACHE_POLICY.md)

本文说明哪些操作允许并发、失败后如何收尾，以及当前不能保证的行为。实现兼容 Python 3.7，核心不依赖第三方库。

## 1. 并发使用规则

| 操作 | 当前行为与要求 |
| --- | --- |
| 拉取多层镜像 | 工作线程分别下载或解码私有文件，最终按 manifest 顺序组装。失败时取消排队任务，并通知下载器协作退出 |
| 同一 RegistryClient 的并发 blob 请求 | token 读写与 401 刷新受锁保护；同一批过期响应复用已刷新的 token。不同客户端各有独立鉴权状态 |
| 同一客户端同时读取不同 manifest | 不支持：`manifest_raw` 表示最近一次响应。多镜像操作需分别创建客户端 |
| 写入同一镜像引用或指令缓存键 | 使用现有线程锁与文件锁保护提交/恢复窗口；竞争通常返回 busy。不同键可并发，库调用不要共享可变构建状态 |
| RUN cache mount | 只支持 `sharing=locked`；按来源目录排序取得文件锁，避免多个挂载互相倒序等待 |
| 清理镜像库或构建缓存 | 仍要求停止共享这些数据的构建、拉取、导入与导出。没有全库读写租约，不能与它们并发执行 prune |
| 发布交付文件 | 输出路径须不存在；单个文件通过发布事务处理冲突和回滚。多个附属文件不是面向并发读者的整体原子快照 |

进程内引用锁使用弱引用表：最后一个持有者离开后，历史键不再永久占用内存。磁盘锁文件保留，以保持文件锁身份稳定；不要在线删除它们来节省空间。

## 2. 下载失败与取消

收尾顺序为：**通知取消 → 取消排队 Future → 等待工作线程 → 清理临时目录 → 回滚本次发布**。

- Artifactory 的并发闸门会唤醒等待者，使其退出；重试开始前及响应读取前后检查取消。读取返回后若已取消，不再写入该块；关闭流并删除本次部分文件，撤回相应进度计数，不把取消当成可重试网络异常。两次重试的等待期间收到取消，也由整个尝试循环统一清理；尚未开始本次请求时，取消清理不删除已有文件。
- 文件总大小、Content-Length 或 Content-Range 已知时，按最小有效边界限制读取；多读一个字节即可发现超长响应，丢弃错误前缀后再重试。失败产生的空文件也会删除，不能在期望大小为零时误命中大小缓存。
- Registry 在请求开始、鉴权重试以及响应读取前后检查取消，包括 EOF、最后一次进度回调及 manifest/token 等 JSON 响应；取消不能因没有下一块数据而被漏掉。底层 blob 请求失败时仍可能留有部分文件，由调用方在等待线程退出后清理。客户端一旦 cancel，不能继续用于下一次操作，应新建客户端。
- Registry 请求始终先关闭 HTTPResponse，再关闭 HTTPConnection；HTTP/1.0 或 Connection: close 的响应可能通过 makefile 单独持有 socket，连接对象关闭不等于未读完的响应已经释放。取消、大小拒绝和 JSON 校验失败也执行这套关闭顺序。
- 已阻塞的网络读取仍可能等待 socket 超时；解压、哈希可能继续到本次调用结束。没有任何可信长度声明时，下载大小仍以 EOF 为准，没有统一的镜像大小上限。取消不是强制停止线程，也没有固定的退出时限。
- 临时目录必须在工作线程结束后清理，避免线程继续写入已删除目录或下一次构建的路径。

## 3. 进度线程与输出错误

`BuildReporter.start()` 只允许调用一次。`success()`、`fail()` 和 `close()` 都结束心跳，关闭渲染器；并发终态调用最多发布一个终态事件。join 在事件锁外进行，未启动线程和当前线程不被 join。后台输出失败由下一次前台输出报告，不能静默当成成功。

每一代心跳绑定独立的停止事件。暂停时永久停止旧代，恢复时创建新事件；恢复过程的线程启动握手被中断后再重试，也不会清除旧代的停止标记、复活已失去句柄的心跳线程。

暂停体已抛出构建错误或 KeyboardInterrupt 时，恢复心跳的异常不会替换原始异常；仅恢复失败时仍报告启动错误。`close()` 在异常收尾期间优先保留已有异常，继续停止并回收心跳；没有正在处理的异常时，关闭输出失败仍会抛出。报告器不长期保存被忽略的次级收尾异常。

CLI 在 finally 中调用 close，覆盖主线程异常、KeyboardInterrupt 和 WSL 提前返回。库调用者仍需负责资源生命周期：

```python
reporter.start(tag, platform)
try:
    build(..., reporter=reporter)
    reporter.success()
except Exception as error:
    reporter.fail(error)
    raise
finally:
    reporter.close()
```

自定义 renderer 或日志回调必须及时返回；无限阻塞的回调无法由报告器强制终止。关闭后释放记录的渲染异常，避免 traceback 长期引用失败构建对象。Artifactory 打包也在发布 tar 前回收心跳线程，并检查后台渲染是否失败。

层任务统一使用 `progress/layers.py`。每组独立锁保护开始、完成、失败集合和事件顺序；并发层可以乱序结束，已完成数量按集合计数。只有全部成功才能发布组完成，零层组发布 `0/0`。字节进度可以节流，开始、完成和失败事件不会被节流；失败事件的输出异常不能覆盖原始处理异常。组完成仅代表该组工作完成，不能代替引用发布、输出归档或整个构建成功。

## 4. Linux RUN 的线程与进程

Python 官方明确指出，多线程进程中的 fork 不安全；Python 3.12 会对此发出警告。[Python os.fork 文档](https://docs.python.org/3.12/library/os.html#os.fork)

构建器在 RUN 前暂停并回收自己的心跳线程，RUN 后恢复。执行器在管道分配前拒绝存在其他活跃 Python 线程的调用。RUN 期间 stdout/stderr 日志仍会输出，安静命令没有周期心跳。

这不是任意宿主中的 fork 安全保证：原生 C 线程及并发创建线程不能由这个检查完整覆盖。含 RUN 的集成请使用独立 Python CLI 进程。普通无 RUN 构建不受这一限制。

父进程遇到取消或准备错误时，请求 supervisor 清理 namespace init，回收 supervisor，再清理管道和日志线程。supervisor 先确认 runner 尚未被回收，再发送 SIGKILL、waitpid；不会向已回收的旧 PID 发信号。Linux 的 PID namespace init 被终止后，内核会终止其中其他进程，包括另建 session 的后台程序。[Linux PID namespaces 文档](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)

日志线程通过锁保护的交接表领取管道，父进程也在同一把锁下撤销未领取的交接，再关闭仍归自己所有的描述符。`ident` 只用于判断是否可以 join，不能证明操作系统尚未创建线程。线程启动握手被中断时，迟启动的线程不会打开已释放、可能已被其他操作复用的描述符。

尚未完成 Python bootstrap 的原生线程不能立即 join；它恢复调度后会看到交接撤销或自己那一代的停止事件并退出。短暂存续期间，执行器的活跃线程检查仍可能拒绝新的 RUN，不承诺强制终止线程调度。

该流程已有系统调用替身测试，真实内核行为仍需下一节验收。不保证硬杀父进程、supervisor 被外部 SIGKILL、机器断电、重复中断或长期不可中断内核调用后的清理。Windows 到 WSL 的外层取消也需单独现场验证。

## 5. 凭据与秘密

| 输入 | 处理与边界 |
| --- | --- |
| 仓库密码/token | 使用环境变量；不写入配置文件。仍会在本次客户端内存中存在，不承诺安全擦除 Python 字符串 |
| Registry 鉴权与重定向 | 自动鉴权和 401 重试均按不区分大小写的字段名替换 Authorization，不能同时发送旧头与新 Bearer 头。转向其他来源时，过滤显式或自动 Authorization、Proxy-Authorization、Cookie。Bearer token 服务必须是已信任来源 |
| Artifactory URL | 拒绝内嵌用户名/密码；凭据仍通过独立参数提供，同源限制继续生效 |
| RUN secret mount | 临时挂载，层差异排除挂载路径；已有规则禁止复用含 secret 的 RUN 指令缓存。命令主动复制或打印秘密仍会泄露 |
| Dockerfile、ARG、COPY、日志与 URL 查询参数 | 不提供通用内容脱敏。不要把秘密放进普通构建参数、上下文或来源 URL；进度、history、镜像内容及缓存可能保存这些输入 |

共享锁保护合作进程的操作顺序，不能抵御另一个进程绕过工具直接替换文件。构建期间仍须保持输入稳定；恶意并发修改宿主目录不在支持范围。

## 6. 验证方法

无需 Docker 的专项测试：

```text
python -m unittest discover -s tests -p test_concurrency_lifecycle.py -v
python -m unittest discover -s tests -p test_run_parent_cleanup.py -v
```

它们使用真实线程、锁、Future、文件和管道；网络及 Linux 系统调用使用本地替身，不代表生产服务或真实 namespace 验收。

在具备 namespace/OverlayFS 权限的 Linux 构建机，提供匹配宿主架构、含 `/bin/sh` 和 `sleep` 的基础 Docker archive：

```text
python3 tests/linux_run_lifecycle_smoke.py --base-tar /images/base.tar --from example/base:1 --sandbox hardened
python3 tests/linux_run_lifecycle_smoke.py --base-tar /images/base.tar --from example/base:1 --sandbox hardened --detached
```

`hardened` 需要 root；`rootless` 按现有单 UID/GID 和内核规则执行。`--detached` 额外要求基础镜像有 `setsid`。脚本在 RUN 启动后用主线程信号计时器取消，检查后代 PID、直接子进程、线程、描述符和未发布归档。没有可用 Linux/WSL 时只能检查脚本语法和 CLI，不能报告这项验收通过。

完整回归、离线兼容性与内网验收方法见 [开发与维护](../CONTRIBUTING.md)。
