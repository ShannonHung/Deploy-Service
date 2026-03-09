"""
app/core/log_viewer_template.py

Premium HTML/CSS/JS template for GitLab job log viewing.
Supports light and dark mode with toggle, and overrides hard-to-read ANSI blue.
"""

LOG_VIEWER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Job Log Viewer | {job_id}</title>
    <style>
        /* CSS Variables - Default (Dark Theme) */
        :root {{
            --bg-color: #0d1117;
            --header-bg: #161b22;
            --text-color: #c9d1d9;
            --accent-color: #58a6ff;
            --border-color: #30363d;
            --line-number-color: #484f58;
            --success-color: #238636;
            --hover-bg: rgba(255, 255, 255, 0.04);
            /* Override Ansi2HTML hardcoded colors for dark mode readability */
            --ansi-black: #484f58;       /* Map pure black to gray */
            --ansi-red: #ff7b72;
            --ansi-green: #3fb950;
            --ansi-yellow: #d29922;
            --ansi-blue: #58a6ff;
            --ansi-magenta: #bc8cff;
            --ansi-cyan: #39c5cf;
            --ansi-white: #b1bac4;
            --ansi-bright-black: #8b949e;
        }}

        /* Light Theme overrides */
        [data-theme="light"] {{
            --bg-color: #ffffff;
            --header-bg: #f6f8fa;
            --text-color: #24292f;
            --accent-color: #0969da;
            --border-color: #d0d7de;
            --line-number-color: #8c959f;
            --success-color: #2da44e;
            --hover-bg: rgba(0, 0, 0, 0.04);
            /* Light theme ANSI colors */
            --ansi-black: #24292f;
            --ansi-red: #cf222e;
            --ansi-green: #116329;
            --ansi-yellow: #9a6700;
            --ansi-blue: #0969da;
            --ansi-magenta: #8250df;
            --ansi-cyan: #1b7c83;
            --ansi-white: #6e7781;
            --ansi-bright-black: #57606a;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-color);
            color: var(--text-color);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            transition: background-color 0.2s, color 0.2s;
        }}

        header {{
            background-color: var(--header-bg);
            border-bottom: 1px solid var(--border-color);
            padding: 10px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            z-index: 100;
            transition: background-color 0.2s, border-color 0.2s;
        }}

        .title-group {{ display: flex; align-items: center; gap: 12px; }}
        .badge {{
            background: var(--accent-color);
            color: white;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        h1 {{ font-size: 16px; font-weight: 600; }}

        .controls {{ display: flex; gap: 16px; align-items: center; }}
        
        .theme-toggle {{
            background: none;
            border: 1px solid var(--border-color);
            color: var(--text-color);
            padding: 4px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s;
        }}
        .theme-toggle:hover {{
            background: var(--hover-bg);
            border-color: var(--line-number-color);
        }}

        .status {{ font-size: 13px; color: var(--line-number-color); display: flex; align-items: center; gap: 10px; }}
        .dot {{ width: 8px; height: 8px; background: var(--success-color); border-radius: 50%; display: inline-block; animation: pulse 2s infinite; }}
        .job-status-badge {{ background: var(--hover-bg); border: 1px solid var(--border-color); padding: 3px 10px; border-radius: 12px; font-size: 11px; color: var(--text-color); font-weight: 600; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 0.4; }} 50% {{ opacity: 1; }} }}

        #log-container {{
            flex: 1;
            overflow: auto;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 13px;
            background: var(--bg-color);
            padding-bottom: 20px;
        }}

        .log-table {{ display: table; width: 100%; border-collapse: collapse; }}
        .log-line {{ display: table-row; line-height: 1.5; }}
        .log-line:hover {{ background: var(--hover-bg); }}

        .cell {{ display: table-cell; padding: 2px 4px; vertical-align: top; border-bottom: none; }}
        .line-num {{
            color: var(--line-number-color);
            text-align: right;
            width: 50px;
            min-width: 50px;
            border-right: 1px solid var(--border-color);
            user-select: none;
            padding-right: 10px;
            padding-left: 10px;
        }}
        .content {{ padding-left: 12px; width: 100%; white-space: pre-wrap; word-break: break-all; }}

        /* Scrollbar */
        ::-webkit-scrollbar {{ width: 12px; height: 12px; }}
        ::-webkit-scrollbar-track {{ background: var(--bg-color); }}
        ::-webkit-scrollbar-thumb {{ background: var(--border-color); border-radius: 6px; border: 3px solid var(--bg-color); }}
        ::-webkit-scrollbar-thumb:hover {{ background: var(--line-number-color); }}

        /* Ansi2HTML dynamically overrides using inline styles, but we can target specific classes if we use full=False without inline=True. Since we use inline=True, we use JS to replace the hard-to-read blue. */
        .ansi-override-blue {{ color: var(--ansi-blue) !important; }}
    </style>
</head>
<body>
    <header>
        <div class="title-group">
            <span class="badge">Job Trace</span>
            <h1>Job: {job_id}</h1>
        </div>
        <div class="controls">
            <button class="theme-toggle" id="theme-btn" onclick="toggleTheme()">
                <span id="theme-icon">☀️</span> Light Mode
            </button>
            <div class="status">
                <span id="job-status" class="job-status-badge">...</span>
                <span class="dot" id="polling-dot"></span>
                <span id="sync-status">Connecting...</span>
            </div>
        </div>
    </header>

    <div id="log-container">
        <div class="log-table" id="log-table"></div>
    </div>

    <script>
        const table = document.getElementById('log-table');
        const container = document.getElementById('log-container');
        const syncStatus = document.getElementById('sync-status');
        const jobStatusBadge = document.getElementById('job-status');
        const themeBtn = document.getElementById('theme-btn');
        const themeIcon = document.getElementById('theme-icon');
        const pollingDot = document.getElementById('polling-dot');
        
        let currentOffset = 0;
        let timer = null;
        let isDarkMode = true;
        
        const TERMINAL_STATUSES = ['success', 'failed', 'canceled', 'skipped', 'manual'];

        // Apply theme on load
        if (localStorage.getItem('theme') === 'light') {{
            setTheme('light');
        }}

        function toggleTheme() {{
            if (isDarkMode) {{
                setTheme('light');
            }} else {{
                setTheme('dark');
            }}
        }}

        function setTheme(theme) {{
            if (theme === 'light') {{
                document.documentElement.setAttribute('data-theme', 'light');
                themeIcon.innerText = '🌙';
                themeBtn.innerHTML = '<span id="theme-icon">🌙</span> Dark Mode';
                isDarkMode = false;
                localStorage.setItem('theme', 'light');
            }} else {{
                document.documentElement.removeAttribute('data-theme');
                themeIcon.innerText = '☀️';
                themeBtn.innerHTML = '<span id="theme-icon">☀️</span> Light Mode';
                isDarkMode = true;
                localStorage.setItem('theme', 'dark');
            }}
        }}

        async function refresh() {{
            try {{
                const res = await fetch(`/api/v1/deploy/jobs/{job_id}/trace/ui?offset=${{currentOffset}}&t=${{Date.now()}}`, {{ cache: 'no-store' }});
                const json = await res.json();
                if (!json.data || !json.data.lines) throw new Error("Invalid API response");

                const data = json.data;
                const status = data.status || 'unknown';
                jobStatusBadge.innerText = status.toUpperCase();

                if (TERMINAL_STATUSES.includes(status)) {{
                    if (timer) clearInterval(timer);
                    timer = null;
                    pollingDot.style.animation = 'none';
                    pollingDot.style.opacity = '0.5';
                    syncStatus.innerText = `Finished`;
                }} else {{
                    syncStatus.innerText = `Syncing...`;
                }}

                if (data.lines.length === 0) return;
                currentOffset = data.next_offset;

                render(data.lines);
                
                // Auto-scroll logic (always on since we removed toggles)
                container.scrollTop = container.scrollHeight;
            }} catch (e) {{
                syncStatus.innerText = "Sync Failed";
                console.error(e);
            }}
        }}

        function render(lines) {{
            let html = '';
            lines.forEach(l => {{
                // Replace Ansi2HTML hardcoded hex colors with our themeable CSS variables
                let content = l.content_html
                    .replace(/color: #000000/gi, 'color: var(--ansi-black)')
                    .replace(/color: #aa0000/gi, 'color: var(--ansi-red)')
                    .replace(/color: #00aa00/gi, 'color: var(--ansi-green)')
                    .replace(/color: #aaaa00/gi, 'color: var(--ansi-yellow)')
                    .replace(/color: #0000aa/gi, 'color: var(--ansi-blue)')
                    .replace(/color: #aa00aa/gi, 'color: var(--ansi-magenta)')
                    .replace(/color: #00aaaa/gi, 'color: var(--ansi-cyan)')
                    .replace(/color: #aaaaaa/gi, 'color: var(--ansi-white)')
                    .replace(/color: #555555/gi, 'color: var(--ansi-bright-black)');
                
                html += `
                    <div class="log-line">
                        <div class="cell line-num">${{l.num}}</div>
                        <div class="cell content">${{content}}</div>
                    </div>
                `;
            }});
            table.insertAdjacentHTML('beforeend', html);
        }}

        refresh();
        timer = setInterval(refresh, 5000);
    </script>
</body>
</html>
"""
