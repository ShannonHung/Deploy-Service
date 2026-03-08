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
        section_stack = []

        pending_section_start_id = None

        for i, raw_line in enumerate(lines):
            line = raw_line
            timestamp = ""

            # 1. Extract Timestamp
            ts_match = _TS_REGEX.match(line)
            if ts_match:
                ts_raw = ts_match.group(2)
                timestamp = ts_raw
                line = line[len(ts_match.group(1)):].strip()
            
            # 1.5 Clean log multiplexer noise (e.g., 00O, 01O+, 0K, 00O+ )
            line = re.sub(r"^\d{1,2}[A-Za-z][\+\-]?\s*", "", line)

            # 2. Extract Section Info BEFORE \r flattening clobbers it
            start_match = re.search(r"section_start:(\d+):([^\r\n\u001b\[]+)", line)
            end_match = re.search(r"section_end:(\d+):([^\r\n\u001b\[]+)", line)

            if start_match:
                section_name = start_match.group(2)
                section_stack.append(section_name)
                pending_section_start_id = section_name
                # Strip only the marker
                line = re.sub(r"section_start:\d+:[^\r\n\u001b\[]+(?:\r|(?:\x1b\[0K))?", "", line)

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

            line = line.strip()

            # Clean structural markers and any residual ANSI sequences
            line = _MARKER_REGEX.sub("", line).strip()
            line = re.sub(r"^\+ \x1b\[0K", "+ ", line)
            line = re.sub(r"^\x1b\[0K\x1b\[\d+;\d+m", "", line)
            line = re.sub(r"^\x1b\[0K", "", line)
            line = re.sub(r"^\+ ", "", line).strip()

            # 4. Convert ANSI to HTML
            content_html = self._conv.convert(line, full=False)
            
            # Skip empty lines, unless it's a section end which needs to pop the stack
            if not content_html.strip():
                if end_match and section_stack:
                    section_stack.pop()
                continue
            
            # Determine header status
            is_section_header = False
            if pending_section_start_id:
                is_section_header = True
                current_sid = pending_section_start_id
                pending_section_start_id = None
            else:
                current_sid = section_stack[-1] if section_stack else None
                
            # Calculate depth
            if is_section_header:
                depth = max(0, len(section_stack) - 1)
            else:
                depth = len(section_stack)

            result.append(FormattedLogLine(
                num=len(result) + 1,
                timestamp=timestamp,
                content_html=content_html,
                is_section_header=is_section_header,
                is_section_end=bool(end_match),
                section_id=current_sid,
                depth=depth
            ))

            if end_match and section_stack:
                section_stack.pop()

        return result
