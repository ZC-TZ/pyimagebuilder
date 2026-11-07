"""为 CI 和重定向日志提供稳定的逐行控制台输出。"""

import sys

from .progress_bar import progress_text, size
from .renderer_base import Renderer
from .layers import LAYER_EVENTS, format_layer_event
from .pull import format_pull_status


class PlainRenderer(Renderer):
    """把构建事件渲染为适合重定向保存的日志行。"""
    def __init__(self, stream=None, quiet=False, verbose=False, error_stream=None):
        self.stream = stream or sys.stdout
        self.error_stream = error_stream or sys.stderr
        self.quiet = quiet
        self.verbose = verbose

    def _line(self, value):
        print(value, file=self.stream, flush=True)

    def render(self, event):
        """以稳定的逐行格式输出当前事件。"""
        kind = event.type
        if self.quiet and kind not in ("build_success", "build_failed"):
            return
        if kind == "pull_status":
            self._line("    " + format_pull_status(event.details))
        elif kind in LAYER_EVENTS:
            if kind != "layer_start" or self.verbose:
                self._line(format_layer_event(event))
        elif kind == "build_start":
            self._line("PyBuilder | Building {} [{}]".format(
                event.details.get("image", "image"), event.details.get("platform", "unknown")))
        elif kind == "phase_start":
            self._line("[+] {}".format(event.message))
        elif kind == "phase_success":
            self._line("    OK {} ({:.2f}s)".format(event.message or event.phase, event.duration or 0))
        elif kind == "step_start":
            instruction = event.instruction or ""
            if "\n" in instruction:
                instruction = instruction.splitlines()[0] + " ..."
            self._line("[{}/{}] {}".format(event.step, event.total, instruction))
        elif kind == "step_cache_hit":
            self._line("    CACHED {}".format(event.message))
        elif kind == "step_cache_miss":
            self._line("    CACHE MISS")
        elif kind == "step_log":
            self._line("    > {}".format(event.message))
        elif kind == "step_progress":
            self._line("    {}".format(progress_text(event)))
        elif kind == "heartbeat":
            if event.details.get("pull"):
                self._line("    ... " + format_pull_status(event.details["pull"]))
            else:
                self._line("    ... {} ({:.1f}s)".format(event.message, event.duration or 0))
        elif kind == "step_success":
            suffix = " ({:.2f}s)".format(event.duration or 0)
            self._line("    OK {}{}".format(event.message or "done", suffix))
        elif kind == "step_failed":
            self._line("    FAILED {} (Dockerfile:{}, {:.2f}s)".format(
                event.message, event.line or "?", event.duration or 0))
        elif kind == "build_failed":
            label = " at step {}/{}".format(event.step, event.total) if event.step else ""
            print("Build failed{}: {}".format(label, event.message), file=self.error_stream, flush=True)
            if event.line:
                print("Dockerfile:{}".format(event.line), file=self.error_stream, flush=True)
            print("Duration: {:.2f}s".format(event.duration or 0), file=self.error_stream, flush=True)
        elif kind == "build_success":
            summary = event.details
            self._line("Build succeeded")
            for name, key in (("Image", "image"), ("Platform", "platform"),
                              ("Output", "output"), ("Digest", "digest"),
                              ("Size", "size"), ("Layers", "layers"),
                              ("Cache", "cache")):
                value = summary.get(key)
                if value is not None:
                    self._line("{:<10} {}".format(name + ":", size(value) if key == "size" else value))
            for key in ("sbom", "provenance", "signature", "oci_output"):
                if summary.get(key):
                    self._line("{:<10} {}".format(key.capitalize() + ":", summary[key]))
            if summary.get("timings"):
                self._line("Stage timings (seconds):")
                self._line("  注意：包含嵌套或并行操作，不能直接相加；总耗时以 Duration 为准。")
                for label, seconds in summary["timings"].items():
                    note = ""
                    if label == "Prepare Artifactory layers (overlap)":
                        note = "（与下载并行的墙钟跨度，不相加）"
                    elif label == "Wait for Artifactory layers":
                        note = "（下载结束后的剩余等待）"
                    self._line("  {:<29} {:>8.2f} {}".format(label, seconds, note).rstrip())
            self._line("Duration:  {:.2f}s".format(event.duration or 0))
