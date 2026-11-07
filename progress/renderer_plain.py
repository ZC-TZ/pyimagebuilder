"""为 CI 和重定向日志提供稳定的逐行控制台输出。"""

import sys

from .progress_bar import progress_text, size
from .renderer_base import Renderer
from .layers import LAYER_EVENTS, format_layer_event
from .pull import format_pull_status
from .presentation import phase_name, stream_text, timing_lines


class PlainRenderer(Renderer):
    """把构建事件渲染为适合重定向保存的日志行。"""
    def __init__(self, stream=None, quiet=False, verbose=False, error_stream=None):
        self.stream = stream or sys.stdout
        self.error_stream = error_stream or sys.stderr
        self.quiet = quiet
        self.verbose = verbose
        self._last_heartbeat = None

    def _line(self, value):
        print(stream_text(value, self.stream), file=self.stream, flush=True)

    def _error_line(self, value):
        print(stream_text(value, self.error_stream), file=self.error_stream, flush=True)

    def render(self, event):
        """以稳定的逐行格式输出当前事件。"""
        kind = event.type
        if self.quiet and kind not in ("build_success", "build_failed"):
            return
        if kind in ("phase_start", "step_start") or (kind == "pull_status" and event.details.get("milestone")):
            self._last_heartbeat = None
        if kind == "heartbeat" and not self.verbose:
            if self._last_heartbeat is not None and event.elapsed - self._last_heartbeat < 5:
                return
            self._last_heartbeat = event.elapsed
        if kind == "pull_status":
            self._line("    " + format_pull_status(event.details))
        elif kind in LAYER_EVENTS:
            if kind != "layer_start" or self.verbose:
                self._line(format_layer_event(event))
        elif kind == "build_start":
            self._line("PyBuilder | Building {} [{}]".format(
                event.details.get("image", "image"), event.details.get("platform", "unknown")))
        elif kind == "phase_start":
            self._line("[+] {}".format(phase_name(event.message)))
        elif kind == "phase_success":
            self._line("    OK {} ({:.2f}s)".format(phase_name(event.message or event.phase), event.duration or 0))
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
                self._line("    ... {} ({:.1f}s)".format(phase_name(event.message), event.duration or 0))
        elif kind == "step_success":
            suffix = " ({:.2f}s)".format(event.duration or 0)
            self._line("    OK {}{}".format(event.message or "done", suffix))
        elif kind == "step_failed":
            self._line("    FAILED {} (Dockerfile:{}, {:.2f}s)".format(
                event.message, event.line or "?", event.duration or 0))
        elif kind == "build_failed":
            label = " at step {}/{}".format(event.step, event.total) if event.step else ""
            self._error_line("Build failed{}: {}".format(label, event.message))
            if event.line:
                self._error_line("Dockerfile:{}".format(event.line))
            self._error_line("Duration: {:.2f}s".format(event.duration or 0))
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
                for line in timing_lines(summary["timings"]):
                    self._line(line)
            self._line("Duration:  {:.2f}s".format(event.duration or 0))
