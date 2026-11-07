"""人类可读的名称、时长和列宽；机器事件仍保留原始字段及名称。"""

import math
import unicodedata


PHASE_NAMES = {
    "Resolving base image": "准备基础镜像", "Resolving base images": "准备基础镜像",
    "Preparing application package": "准备应用包", "Preparing build": "解析构建输入",
    "Exporting image archive": "写出镜像归档", "Verifying image archive": "校验镜像归档",
    "Verifying output digest": "计算最终归档摘要",
}
TIMING_NAMES = {
    "Prepare WAR/dist package": "准备应用包", "List/auth Artifactory": "列目录与鉴权",
    "Probe Artifactory files": "探测文件大小", "Download Artifactory files": "下载基础镜像文件",
    "Prepare Artifactory layers (overlap)": "准备镜像层 [并行]",
    "Wait for Artifactory layers": "等待剩余层", "Import Artifactory CAS blobs": "CAS 入库与提交引用",
    "Convert Artifactory layers": "转换镜像层", "Pack Artifactory base archive": "打包基础镜像归档",
    "Read and index base CAS": "读取基础镜像与索引", "Read and index base archive": "读取基础 tar 与索引",
    "Layer cache lookup": "检查指令缓存", "Layer cache store": "保存指令缓存",
    "COPY/ADD layer creation": "生成 COPY/ADD 层", "RUN layer creation": "执行 RUN 并生成层",
    "Other layer creation": "生成其他文件系统层",
    "Write Docker archive": "写出 Docker tar", "Verify Docker archive": "校验 Docker tar",
    "Publish Docker archive": "发布 Docker tar", "Write OCI archive": "写出 OCI 归档",
    "Verify OCI archive": "校验 OCI 归档", "Publish OCI archive": "发布 OCI 归档",
    "Hash final output archive": "计算最终文件 SHA-256",
    "Registry auth and manifest": "Registry 鉴权与元数据", "Download Registry config": "下载镜像配置",
    "Download Registry layers": "下载 Registry 层", "Decompress and verify Registry layers": "解压并校验 Registry 层",
    "Write pulled Docker archive": "写出下载归档", "Verify pulled Docker archive": "校验下载归档",
    "Write pulled OCI archive": "写出下载 OCI 归档", "Verify pulled OCI archive": "校验下载 OCI 归档",
}


def phase_name(value):
    return PHASE_NAMES.get(value, value)


def terminal_width(value):
    """按显示列计算中文和组合字符宽度，供表格与临时状态共用。"""
    return sum(0 if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf")
               else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in value)


def clock(seconds):
    """相对任务开始的分钟/小时刻度；未知时间不伪装成任务起点。"""
    if type(seconds) not in (int, float) or not 0 <= seconds <= 10 ** 9 or not math.isfinite(seconds):
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return "{:02d}:{:02d}:{:02d}".format(seconds // 3600, seconds // 60 % 60, seconds % 60)
    return "{:02d}:{:02d}".format(seconds // 60, seconds % 60)


def timing_lines(timings):
    """按操作耗时排序；条形仅相对本表最长项，不表示总耗时占比。"""
    rows = [(TIMING_NAMES.get(name, name), float(seconds), name) for name, seconds in timings.items()]
    if any(not math.isfinite(seconds) or seconds < 0 for _, seconds, _ in rows):
        raise ValueError("Invalid stage duration")
    rows.sort(key=lambda row: (-row[1], row[0]))
    width = max([20] + [terminal_width(label) for label, _, _ in rows])
    longest = max([0] + [seconds for _, seconds, _ in rows])
    for label, seconds, name in rows:
        count = min(12, max(1, round(seconds / longest * 12))) if seconds > 0 and longest else 0
        note = {"Prepare Artifactory layers (overlap)": "  与下载并行的墙钟跨度，不相加",
                "Wait for Artifactory layers": "  下载结束后的剩余等待"}.get(name, "")
        yield "  {}{}  {:>8.2f} s  {}{}".format(
            label, " " * (width - terminal_width(label)), seconds, "#" * count, note).rstrip()
