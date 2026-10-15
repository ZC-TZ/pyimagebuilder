# 构建进度输出

[文档目录](README.md) · [项目首页](../README.md)

以下是当前 `PlainRenderer` 的输出格式示例。路径、镜像名和 digest 已缩写；RUN 和私有仓库示例展示相应事件的渲染格式，本机 Windows 环境未执行 Linux RUN 或连接内网仓库。

## 1. 完全命中层缓存

```text
PyBuilder | Building demo/app:1 [linux/amd64]
[+] Preparing build
    > Dockerfile parsed: 3 instruction(s)
    OK Preparing build (0.00s)
[1/3] FROM example/base:1
    > Base archive: C:\images\base.tar
    OK done (0.01s)
[2/3] COPY payload /app/payload
    CACHED sha256:1b2c...
    OK done (0.00s)
[3/3] ENV A=B
    OK metadata only (0.00s)
[+] Exporting image archive
    OK Exporting image archive (0.03s)
[+] Verifying output digest
    OK Verifying output digest (0.01s)
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
[+] Resolving base image
    > Using local base D:\images\tongweb.tar
    OK Resolving base image (0.02s)
[+] Preparing application package
    OK Preparing application package (0.01s)
[+] Preparing build
    > Dockerfile parsed: 3 instruction(s)
    OK Preparing build (0.00s)
[1/3] FROM example/tongweb:11
[2/3] COPY --chown=tongadmin:tonggrp app.war /soft/TongWeb7.0/autodeploy/sfcx.war
[3/3] COPY --chown=tongadmin:tonggrp tongweb.xml /soft/TongWeb7.0/conf/tongweb.xml
[+] Exporting image archive
...
Build succeeded
Image:     sfcx_back:1.3.4
```

## 4. 首次下载基础镜像

```text
[+] Resolving base images
    > Resolving registry.example.com/team/base:1 (linux/amd64)
    > Registry layer 1/7
    Downloading Registry blob 72% (552.0 MiB/766.0 MiB)
    ... Resolving base images (8.0s)
    > Pulled base registry.example.com/team/base:1 (linux/amd64)
    OK Resolving base images (12.13s)
```

Artifactory 来源会显示 URL、下载文件进度、格式转换与打包状态；第二次构建会显示 `Cached base ...`。

### 统一的层任务事件

Artifactory（CAS 和 Docker tar）、Registry、CAS 读取、本地 tar 读取与构建索引共用
`progress.layers.LayerProgress`。每层固定发出开始和成功/失败事件，中间字节事件逐层节流；
小层不会因为不到五秒而缺失完成记录。缓存命中仍校验数据，标记“复用缓存”，不会伪装成重新下载。

```text
    > 准备镜像层 1/21 | 开始
    OK 准备镜像层 1/21 | 完成 | 264.0 MiB | 5.20s | 已完成 1/21
    > 准备镜像层 2/21 | 开始
    OK 准备镜像层 2/21 | 完成 | 8.0 MiB | 0.12s | 已完成 2/21
    ...
    OK 准备镜像层完成 | 21/21 层
    > 校验基础层 1/21 | 开始
    OK 校验基础层 1/21 | 完成（复用缓存） | 264.0 MiB | 0.30s | 已完成 1/21
```

层序号表示 manifest 中的位置；“已完成”才是本组实际完成数量。并发时第21层可能先完成，
此时显示“21/21，已完成1/21”，不能把它当成全部完成。零层组明确汇总为0/0。
失败/取消不发该层成功事件，失败或漏处理的组不能发成功汇总；错误输出再次失败也不替换原始错误。
TTY 中间状态按终端列宽刷新，完成行持久保存；普通日志不覆盖行。

| JSON type | 含义 |
| --- | --- |
| `layer_start` | 单层任务开始 |
| `layer_progress` | 单层字节进度，独立节流 |
| `layer_complete` | 本层操作成功，含大小、耗时、缓存复用状态 |
| `layer_failed` | 失败或取消，保留当前大小和错误原因 |
| `layers_complete` | 本组所有任务成功，包含零层组 |

这些事件不再塞进 `step_log`。JSON 消费者直接读 `details.group_id`、`operation`、
`layer_index`、`layers_total`、`layers_completed`、`digest`、`cached`、`source`、
`image`，字节数为顶层 `current` / `amount`，耗时为 `duration`。
同一group_id对应同一组任务；阶段间不能把压缩下载字节与解码tar字节相加。

operation 分别为 `download`（下载传输blob）、`prepare`（解包/校验）、
`store`（写CAS）、`verify`（校验本地CAS基础层）、`read`（读取本地基础tar）、
`index`（应用到虚拟rootfs）。Artifactory直接入CAS的prepare还包含解码层入库及临时文件清理，
其汇总在CAS引用导入和可用性检查后发出；Docker tar导出路径分别报告prepare/store。
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
[+] Exporting image archive
    Writing Docker layer 72% (552.0 MiB/766.0 MiB)
    ... Exporting image archive (4.0s)
    Writing Docker layer 100% (766.0 MiB/766.0 MiB)
    OK Exporting image archive (5.02s)
[+] Verifying output digest
    Hashing archive 100% (766.0 MiB/766.0 MiB)
    OK Verifying output digest (0.82s)
Build succeeded
```

`--progress=json` 示例：

```jsonl
{"type":"step_start","elapsed":0.021,"phase":"COPY app.war /app/app.war","step":2,"total":3,"instruction":"COPY app.war /app/app.war","line":2}
{"type":"step_progress","elapsed":0.455,"message":"Copying app/app.war","phase":"COPY app.war /app/app.war","step":2,"total":3,"instruction":"COPY app.war /app/app.war","line":2,"current":10485760,"amount":20971520,"unit":"bytes"}
{"type":"step_success","elapsed":0.892,"message":"done","phase":"COPY app.war /app/app.war","step":2,"total":3,"instruction":"COPY app.war /app/app.war","line":2,"duration":0.871}
```

事件由 `progress.events.BuildEvent` 定义，`progress.reporter.BuildReporter` 发布；console、JSON 和未来的 Web UI 只需实现 `render(event)` / `close()` 接口。高频字节更新最多每 250 毫秒发一次，读取器每约 1 MiB 才调用一次进度回调。CI 和重定向输出不含颜色与回车覆盖符。
