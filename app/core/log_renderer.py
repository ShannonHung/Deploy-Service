"""
app/core/log_renderer.py

Utility for converting ANSI job logs to structured HTML using ansi2html.
"""

from __future__ import annotations

import re
from typing import List
from ansi2html import Ansi2HTMLConverter
from app.domain.pipeline_models import FormattedLogLine

# GitLab style timestamps: [2024-03-08T12:00:00Z] or similar at line start
_TS_REGEX = re.compile(r"^(\[?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\]?)")
# GitLab section markers and clear-line codes (like [0K, [0G)
_MARKER_REGEX = re.compile(r"\x1b\[[0-9;]*[GK]|section_(?:start|end):\d+:[^\r\n\u001b\[]+")

class LogRenderer:
    """Processes raw ANSI logs into structured HTML lines."""

    def __init__(self):
        # inline=True uses style tags within spans, which is safer for direct injection
        self._conv = Ansi2HTMLConverter(inline=True, linkify=True)

    def render(self, job_id: int, raw_text: str) -> List[FormattedLogLine]:
        """Convert raw text to a list of FormattedLogLine objects."""
        # Split by newline only, preserving \r within lines for terminal simulation
        if raw_text.endswith('\n'):
            raw_text = raw_text[:-1]
        lines = raw_text.split('\n')
        result = []

        for i, raw_line in enumerate(lines):
            line = raw_line

            # 1. Remove timestamp prefixes if any (GitLab injects these)
            line = _TS_REGEX.sub("", line)

            # 2. Clean log multiplexer noise (e.g., 00O, 01O+, 0K, 00O+ )
            # Sometimes GitLab runner prepends \x1b[0K (clear line) before the multiplexer noise
            line = re.sub(r"^\x1b\[0K", "", line)
            line = re.sub(r"^\d{1,2}[A-Za-z][\+\-]?\s?", "", line)

            # Strip section markers (but optionally keep the text)
            line = re.sub(r"section_start:\d+:[^\r\n\u001b\[]+(?:\r|(?:\x1b\[0K))?", "", line)
            line = re.sub(r"section_end:\d+:[^\r\n\u001b\[]+(?:\r|(?:\x1b\[0K))?", "", line)

            # 3. Simulate terminal carriage return \r
            if '\r' in line:
                parts = line.split('\r')
                res = parts[0]
                for p in parts[1:]:
                    if not p: continue
                    if len(p) >= len(res):
                        res = p
                    else:
                        res = p + res[len(p):]
                line = res

            # Clean structural markers and any residual ANSI sequences
            # ONLY strip off the right side (trailing newlines/spaces) to preserve left indentation
            line = line.rstrip()
            line = _MARKER_REGEX.sub("", line)
            line = re.sub(r"^\+ \x1b\[0K", "+ ", line)
            line = re.sub(r"^\x1b\[0K\x1b\[\d+;\d+m", "", line)
            line = re.sub(r"^\x1b\[0K", "", line)
            line = re.sub(r"^\+ ", "", line)

            # 4. Convert ANSI to HTML
            content_html = self._conv.convert(line, full=False)
            
            # Skip lines that are completely empty (no html, no whitespace)
            if not content_html.strip():
                continue

            result.append(FormattedLogLine(
                num=len(result) + 1,
                content_html=content_html,
            ))

        return result
