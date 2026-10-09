"""为终端和普通日志共用的字节进度格式化函数。"""


def size(value):
    """将字节数格式化为适合展示的传输大小。"""
    value = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or suffix == "TiB":
            return "{:.0f} {}".format(value, suffix) if suffix == "B" else "{:.1f} {}".format(value, suffix)
        value /= 1024


def progress_text(event, bar=False):
    """根据已完成与总字节数生成简洁的进度文字。"""
    current, total = event.current, event.amount
    if current is None:
        return event.message
    metric = size(current) if event.unit == "bytes" else str(current)
    if total is None or total <= 0:
        return "{} {}".format(event.message, metric).strip()
    total_metric = size(total) if event.unit == "bytes" else str(total)
    percent = min(100, 100 * current / total)
    visual = ""
    if bar:
        filled = min(20, round(percent / 5))
        visual = "[{}{}] ".format("#" * filled, "-" * (20 - filled))
    return "{}{} {:.0f}% ({}/{})".format(visual, event.message, percent, metric, total_metric).strip()
