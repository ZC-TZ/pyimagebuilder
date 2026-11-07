# 构建进度输出

[文档目录](README.md) · [项目首页](../README.md)

以下是当前 `PlainRenderer` 的输出格式示例。路径、镜像名和 digest 已缩写；RUN 和私有仓库示例展示相应事件的渲染格式，本机 Windows 环境未执行 Linux RUN 或连接内网仓库。

## 日志外观与阅读方式

CLI 的已知阶段名以中文显示；阶段耗时按从长到短排列，按显示列对齐中文，条形长度只相对本表最长操作，
不代表总耗时占比。同阶段重复心跳默认最多每五秒输出一次，`--verbose` 可展开；交互终端仍原位刷新。
JSON 事件、阶段标识、`details.timings` 的原始键不翻译，也不因普通日志降噪而减少心跳事件。

普通日志仅对当前输出编码无法表示的字符使用反斜杠转义，UTF-8 终端正常显示中文和 emoji；
TTY 在转义后计算列宽，避免状态行换行。JSON 事件使用 ASCII 转义，解析后的字段值保持不变，
并确保 Unicode 行分隔符不拆开 JSON Lines。此规则只影响进度输出，不重新序列化镜像
manifest/config，也不修改镜像摘要。输出管道断开、磁盘写入失败等 I/O 错误仍正常报告。

Web 日志增加行号、相对任务开始的时间和事件类型配色：阶段为蓝色、完成为绿色、缓存为金色、失败为红色。
颜色来自结构化事件类型；应用命令仅仅输出 `FAILED` 不会被当成构建失败事件。
行号在有界日志窗口淘汰旧行后继续递增，样式元数据与文本同步淘汰；未知时间显示 `--:--`。
暂停翻看、跳到最新以及调整窗口后的跟随行为保持可用。日志导出仍是 UTF-8 纯文本，不包含行号、时间栏或 HTML。
这些样式元数据只保留在当前会话，不改变原有任务摘要或镜像格式。

Web 仅把具有有效非负、有界有限时间值的 JSON 当作结构化事件；时间异常的 JSON
保留为普通日志，避免活动层计时溢出、中断后续收集。任务总耗时包含暂存清理；
清理告警显示实际发生时间，结束后的任务摘要写入告警沿用已知的任务结束时间。

有结构化拉取快照时，Web 另显示当前基础镜像的五段流程条、文件数、层数和当前子阶段时间，
目前用于 Artifactory 直接入 CAS 的路径。没有快照的本地构建不推测或伪造拉取进度。
基础镜像就绪只表示基础镜像可以使用；应用镜像的任务结果仍以最终构建状态为准。

## 1. 完全命中层缓存

```text
PyBuilder | Building demo/app:1 [linux/amd64]
[+] 解析构建输入
    > Dockerfile parsed: 3 instruction(s)
    OK 解析构建输入 (0.00s)
[1/3] FROM example/base:1
    > Base archive: C:\images\base.tar
    OK done (0.01s)
[2/3] COPY payload /app/payload
    CACHED sha256:1b2c...
    OK done (0.00s)
[3/3] ENV A=B
    OK metadata only (0.00s)
[+] 写出镜像归档
    OK 写出镜像归档 (0.03s)
[+] 计算最终归档摘要
    OK 计算最终归档摘要 (0.01s)
Build succeeded
Image:     demo/app:1
Platform:  linux/amd64
Output:    C:\images\app.tar
Digest:    sha256:db43...
Size:      41.0 KiB
Layers:    2
Cache:     1 hit(s), 0 miss(es), 0 stored
Duration:  0.07s
```

## 2. 包含 RUN 的构建

```text
[3/5] RUN echo ready && touch /app/ready
    CACHE MISS
    > ready
    OK done (1.21s)
```

RUN 的 stdout 和 stderr 都作为该步骤的 `step_log` 事件发出，JSON 事件的 `details.stream` 标明来源。

## 3. Fast 构建

```text
PyBuilder | Building sfcx_back:1.3.4 [linux/amd64]
[+] 准备应用包
    OK 准备应用包 (0.01s)
[+] 准备基础镜像
    > Using local base D:\images\tongweb.tar
    OK 准备基础镜像 (0.02s)
[+] 解析构建输入
    > Dockerfile parsed: 3 instruction(s)
    OK 解析构建输入 (0.00s)
[1/3] FROM example/tongweb:11
[2/3] COPY --chown=tongadmin:tonggrp app.war /soft/TongWeb7.0/autodeploy/sfcx.war
[3/3] COPY --chown=tongadmin:tonggrp tongweb.xml /soft/TongWeb7.0/conf/tongweb.xml
[+] 写出镜像归档
...
Build succeeded
Image:     sfcx_back:1.3.4
```

## 4. 首次下载基础镜像

```text
[+] 准备基础镜像
    基础镜像 | 读取并校验 manifest/config | 本阶段 0.0s
    基础镜像 | 下载 10/24 文件，300.0 MiB/905.0 MiB | 准备 5/22 层，运行 2（与下载并行）
    OK 准备镜像层 8/22 | 完成 | 20.0 MiB | 0.50s | 已完成 6/22
    基础镜像 | 网络传输完成 | 准备 19/22 层，运行 2，待处理 1（尚未提交引用）
    OK 准备镜像层完成 | 22/22 层
    基础镜像 | CAS 入库与提交引用 | 已复核 0/22 层，运行 0
    OK 写入镜像层 1/22 | 完成 | 8.0 MiB | 0.02s | 已完成 1/22
    ...
    OK 写入镜像层完成 | 22/22 层
    基础镜像已就绪 | 22/22 层 | 引用已提交并验证
    OK 准备基础镜像 (180.00s)
```

以上为 Artifactory 直接入 CAS 的格式示例，数字不代表实测性能。下载文件数包含 manifest/config 等文件，
层数来自 manifest，两者本来就可能不同。下载结束只表示网络传输结束；层准备、原始 blob 入库、引用提交、
可用性检查和工作区清理全部成功后才显示「基础镜像已就绪」。它仍不代表应用镜像已构建成功。
显式 Docker tar 下载路径继续报告格式转换与归档校验；第二次构建可显示 `Cached base ...`。

### 统一的层任务事件

网页日志复用普通控制台格式，每层保留一条完成或失败记录，显示序号、处理量和耗时。
Artifactory 并行拉取期间，任务状态区同时显示网络与层准备数量，心跳沿用整体状态及当前子阶段时长，
不会仅用最早启动的一层代替整体进度。高频状态不追加到网页日志；阶段切换、单层完成/失败和组汇总会保留。
默认普通 CLI 不输出单层开始行，`--verbose` 可展开；JSON 仍保留所有生命周期事件。
构建结束后日志列出阶段耗时，并明确提示嵌套/并行子项不能直接相加。

导出统计分别列出 `Write Docker/OCI archive`、`Verify Docker/OCI archive` 和
`Publish Docker/OCI archive`。Publish 包含最终排他发布，以及必要的跨盘或无硬链接回退复制；
最终整个交付文件的 SHA-256 仍单列为 `Hash final output archive`。
这些是导出阶段内部的子项，不能再与外层导出总耗时重复相加。

Artifactory 供构建使用的 CAS-only 拉取会先取 manifest/config，再把已下载的层交给
最多两个准备线程；下载进度与层完成记录可能交错，层序号仍是 manifest 中的位置。
例如先看到 `准备镜像层 8/22` 完成是正常的，`已完成 1/22` 才表示实际完成数量。
所有层成功准备和所有传输 blob 入库后才发布引用；任一失败会停止后续任务并恢复旧引用。

这条路径的耗时新增 `Prepare Artifactory layers (overlap)`（准备任务的墙钟跨度）、
`Wait for Artifactory layers`（下载结束后剩余等待）和 `Import Artifactory CAS blobs`。
准备跨度包含与下载重叠的部分，不能与下载耗时直接相加；判断总收益请看总耗时和剩余等待。
原有独立 Docker tar 转换路径继续显示 `Convert Artifactory layers`。
普通输出会在重叠准备与剩余等待两项旁加中文说明，JSON 的 `details.timings` 键保持不变。

独立 Artifactory 下载生成 Docker tar 时，会在私有临时归档完成后显示
「正在校验 Docker 镜像归档…」，核对实际归档的配置、层摘要和 tar 结构，再发布最终路径。
这次检查计入原打包耗时；失败时删除临时归档并恢复旧 CAS 引用。
Fast 使用 CAS 的构建路径不增加中间 tar。

CAS 的 `FROM` 读取会在每层完成结构验证后，立即用同一份成员信息建立文件索引。
因此「校验基础层」和「应用基础层」事件可交错，前者的耗时也包含内部索引回调；
评估这一段请使用 `Read and index base CAS` 的总耗时，不要把嵌套的层耗时再相加。
这仅复用当前读取的头信息，不表示跳过缓存文件的完整摘要校验。
使用输出盘暂存时，Write 包含该私有归档的 fsync；它通过完整验证后才发布。

Artifactory（CAS 和 Docker tar）、Registry、CAS 读取、本地 tar 读取与构建索引共用
`progress.layers.LayerProgress`。每层固定发出开始和成功/失败事件，中间字节事件逐层节流；
小层不会因为不到五秒而缺失完成记录。缓存命中仍校验数据，标记“复用缓存”，不会伪装成重新下载。

```text
    OK 准备镜像层 1/21 | 完成 | 264.0 MiB | 5.20s | 已完成 1/21
    OK 准备镜像层 2/21 | 完成 | 8.0 MiB | 0.12s | 已完成 2/21
    ...
    OK 准备镜像层完成 | 21/21 层
    OK 校验基础层 1/21 | 完成（复用缓存） | 264.0 MiB | 0.30s | 已完成 1/21
```

层序号表示 manifest 中的位置；“已完成”才是本组实际完成数量。并发时第21层可能先完成，
此时显示“21/21，已完成1/21”，不能把它当成全部完成。零层组明确汇总为0/0。
失败/取消不发该层成功事件，失败或漏处理的组不能发成功汇总；错误输出再次失败也不替换原始错误。
TTY 中间状态按终端列宽刷新，过长状态截断以避免自动换行残留；完成行持久保存，普通日志不覆盖行。
终端缩窄后，擦除和补空格同样以当前列宽为限；独立 Artifactory 下载脚本也按中文实际列宽处理。
这避免状态刷新额外换行，不回退终端已因窗口调整而重排的历史内容。
需要完整逐行文本可使用 `--progress=plain`，JSON 数据不截断。

| JSON type | 含义 |
| --- | --- |
| `layer_start` | 单层任务开始 |
| `layer_progress` | 单层字节进度，独立节流 |
| `layer_complete` | 本层操作成功，含大小、耗时、缓存复用状态 |
| `layer_failed` | 失败或取消，保留当前大小和错误原因 |
| `layers_complete` | 本组所有任务成功，包含零层组 |
| `pull_status` | Artifactory CAS 拉取的整体阶段和数量快照 |

这些事件不再塞进 `step_log`。JSON 消费者直接读 `details.group_id`、`operation`、
`layer_index`、`layers_total`、`layers_completed`、`digest`、`cached`、`source`、
`image`，字节数为顶层 `current` / `amount`，耗时为 `duration`。
同一group_id对应同一组任务；阶段间不能把压缩下载字节与解码tar字节相加。
层快照还包含 `layers_active`、`layers_pending`、`layers_failed` 和 `layers_revision`。
同组修订号随开始、完成或失败推进，用于防止调度线程较早取得的快照覆盖更新的工作线程状态。

`pull_status.details.stage` 依次为 `metadata`、`transfer`、`waiting`、`ingest`、`ready`；
`pull_id` 区分每次拉取，包括同一外层阶段里的重试；新拉取重新开始子阶段计时，不继承旧数量。
`milestone=true` 表示需要持久显示的阶段切换，其他快照用于状态区刷新。
文件数与传输字节分别为 `files_completed/files_total`、`bytes_completed/bytes_total`；
准备与入库分别使用 `prepare_*`、`store_*` 计数及组标识，不能把两者当成重复层数相加。
`stage_seconds` 是当前子阶段时长，不是整个基础镜像拉取时长；`eta` 只估算网络剩余时间。
并行层事件及心跳的 `details.pull` 携带当时的整体快照。阶段外继续使用原有单层心跳。
进入或完成 Dockerfile 步骤后清除拉取子状态；报告器关闭后的迟到快照不会重新保留状态。

工作台与兼容性检查启动的 Python 子进程使用 UTF-8 输出，并以 UTF-8 读取，
避免 Windows 默认 GBK 解码中文日志时中断收集。自行通过 subprocess 读取 CLI 时，
也应让子进程的 `PYTHONIOENCODING` 与读取端的 `encoding` 一致。

operation 分别为 `download`（下载传输blob）、`prepare`（解包/校验）、
`store`（写CAS）、`verify`（校验本地CAS基础层）、`read`（读取本地基础tar）、
`index`（应用到虚拟rootfs）。Artifactory直接入CAS的prepare还包含解码层入库及临时文件清理，
prepare 汇总只表示解码层已准备完毕；store 汇总只表示传输 blob 已保存并复核，
二者都不能提前宣称引用已经可用，最终由 `pull_status(stage=ready)` 确认。Docker tar导出路径分别报告prepare/store。
层组汇总不是整个构建成功，归档导出和发布仍须随后完成，最终以build_success为准。

## 5. RUN 失败

```text
[5/10] RUN yum install -y curl
    CACHE MISS
    > Error: package curl not found
    FAILED RUN failed: process exited with code 1 (Dockerfile:17, 3.21s)
Build failed at step 5/10: BuildError: RUN failed: process exited with code 1
Dockerfile:17
Duration: 3.24s
```

默认隐藏内部 Python traceback；使用 `--debug` 时打印完整 traceback。

RUN 的管道或首个 fork 创建失败时，回收此前已分配的描述符，并保留原始错误供报告。
启用日志转发和普通输出模式遵循同一规则；不会把后续清理错误当作最初的创建错误。

RUN 日志回调报错时，停止调用日志回调并继续排空 stdout/stderr，等待进程结束与日志线程收尾。
命令非零退出时优先报告 RUN 错误；命令成功但日志转发失败时也报告构建失败，使用 `--debug` 可查看原始日志异常。
此规则不等同于主线程收到 Ctrl+C 后的进程取消流程。

RUN 的 stdout/stderr 每次最多读取 64 KiB。普通短行保持原有输出形式；超长行或连续无换行输出
会分成多个日志事件，达到分段上限即可转发，UTF-8 字符跨段时不会被错误替换。
各分段在普通控制台中可能显示为多行，在 JSON 模式中是多个 step_log 事件。
事件边界不代表原始输出的行边界，stdout 与 stderr 的全局先后顺序也不作保证。
诊断管道保留量最多 256 KiB：超限时保留前后各 128 KiB，并插入截断提示，继续读取其余内容到 EOF。
这限制的是执行器内部缓冲；自定义日志回调若自行累积全部事件，仍需自行管理内存。

## 6. 导出大镜像

```text
[+] 写出镜像归档
    Writing Docker layer 72% (552.0 MiB/766.0 MiB)
    ... 写出镜像归档 (4.0s)
    Writing Docker layer 100% (766.0 MiB/766.0 MiB)
    OK 写出镜像归档 (5.02s)
[+] 计算最终归档摘要
    Hashing archive 100% (766.0 MiB/766.0 MiB)
    OK 计算最终归档摘要 (0.82s)
Build succeeded
```

`--progress=json` 示例：

```jsonl
{"type":"step_start","elapsed":0.021,"phase":"COPY app.war /app/app.war","step":2,"total":3,"instruction":"COPY app.war /app/app.war","line":2}
{"type":"step_progress","elapsed":0.455,"message":"Copying app/app.war","phase":"COPY app.war /app/app.war","step":2,"total":3,"instruction":"COPY app.war /app/app.war","line":2,"current":10485760,"amount":20971520,"unit":"bytes"}
{"type":"step_success","elapsed":0.892,"message":"done","phase":"COPY app.war /app/app.war","step":2,"total":3,"instruction":"COPY app.war /app/app.war","line":2,"duration":0.871}
```

事件由 `progress.events.BuildEvent` 定义，`progress.reporter.BuildReporter` 发布；console、JSON 和未来的 Web UI 只需实现 `render(event)` / `close()` 接口。高频字节更新最多每 250 毫秒发一次，读取器每约 1 MiB 才调用一次进度回调。CI 和重定向输出不含颜色与回车覆盖符。
