"""
app/core/log_viewer_template.py

Premium HTML/CSS/JS template for GitLab job log viewing.
"""

LOG_VIEWER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Job Log Viewer | {job_id}</title>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@400;600&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #0d1117;
            --header-bg: #161b22;
            --text-color: #c9d1d9;
            --accent-color: #58a6ff;
            --border-color: #30363d;
            --line-number-color: #484f58;
            --timestamp-color: #8b949e;
            --success-color: #238636;
            --section-bg: rgba(56, 139, 253, 0.15);
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-color);
            color: var(--text-color);
            font-family: 'Inter', sans-serif;
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }}

        header {{
            background-color: var(--header-bg);
            border-bottom: 1px solid var(--border-color);
            padding: 8px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            z-index: 100;
        }}

        .title-group {{ display: flex; align-items: center; gap: 12px; }}
        .badge {{
            background: var(--accent-color);
            color: white;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        h1 {{ font-size: 15px; font-weight: 600; }}

        .controls {{ display: flex; gap: 16px; align-items: center; }}
        .control-item {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 13px;
            cursor: pointer;
        }}
        input[type="checkbox"] {{ accent-color: var(--accent-color); cursor: pointer; }}

        #log-container {{
            flex: 1;
            overflow: auto;
            font-family: 'Fira Code', monospace;
            font-size: 13px;
            background: var(--bg-color);
        }}

        .log-table {{ display: table; width: 100%; border-collapse: collapse; position: relative; }}
        .log-line {{ display: table-row; line-height: 1.4; font-size: 12px; }}
        .log-line:hover {{ background: rgba(255,255,255,0.04); }}
        .log-line.section {{ font-weight: 500; font-size: 12px; }}

        .cell {{ display: table-cell; padding: 2px 4px; vertical-align: top; border-bottom: none; }}
        .line-num {{
            color: var(--line-number-color);
            text-align: right;
            width: 44px;
            border-right: 1px solid var(--border-color);
            user-select: none;
            padding-right: 8px;
        }}
        .timestamp {{
            color: var(--timestamp-color);
            width: 220px;
            min-width: 220px;
            border-right: none;
            white-space: nowrap;
            user-select: none;
            padding-left: 8px;
            padding-right: 16px;
        }}
        .content {{ padding-left: 0; word-break: break-all; width: 100%; }}
        
        .no-wrap .content {{ white-space: pre; word-break: normal; }}
        .wrap .content {{ white-space: pre-wrap; }}

        .status {{ font-size: 12px; color: var(--line-number-color); display: flex; align-items: center; gap: 10px; }}
        .dot {{ width: 8px; height: 8px; background: var(--success-color); border-radius: 50%; display: inline-block; animation: pulse 2s infinite; }}
        .job-status-badge {{ background: var(--header-bg); border: 1px solid var(--border-color); padding: 2px 8px; border-radius: 12px; font-size: 11px; color: var(--text-color); margin-right: 8px; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 0.4; }} 50% {{ opacity: 1; }} }}

        /* Sections */
        .section-header {{ cursor: pointer; background: var(--section-bg); font-weight: 600; user-select: none; }}
        .section-header:hover {{ background: rgba(56, 139, 253, 0.25); }}
        .caret {{ display: inline-block; width: 16px; text-align: center; color: var(--accent-color); font-size: 10px; line-height: 1.4; vertical-align: baseline; }}
        .caret::after {{ content: '▶'; display: inline-block; transition: transform 0.2s ease; transform: rotate(90deg); }}
        .collapsed .caret::after {{ transform: rotate(0deg); }}
        .hidden {{ display: none; }}

        /* Scrollbar */
        ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
        ::-webkit-scrollbar-track {{ background: var(--bg-color); }}
        ::-webkit-scrollbar-thumb {{ background: var(--border-color); border-radius: 5px; }}
    </style>
</head>
<body class="wrap">
    <header>
        <div class="title-group">
            <span class="badge">Premium Logs</span>
            <h1>Job ID: {job_id}</h1>
        </div>
        <div class="controls">
            <div class="status">
                <span id="job-status" class="job-status-badge">...</span>
                <span class="dot"></span>
                <span id="sync-status">Connecting...</span>
            </div>
            <label class="control-item"><input type="checkbox" id="wrap-toggle" checked> Wrap</label>
            <label class="control-item"><input type="checkbox" id="scroll-toggle" checked> Scroll</label>
            <label class="control-item"><input type="checkbox" id="refresh-toggle" checked> Refresh</label>
        </div>
    </header>

    <div id="log-container">
        <div class="log-table" id="log-table"></div>
    </div>

    <script>
        const job_id = "{job_id}";
        const table = document.getElementById('log-table');
        const container = document.getElementById('log-container');
        const syncStatus = document.getElementById('sync-status');
        
        const wrapCheck = document.getElementById('wrap-toggle');
        const scrollCheck = document.getElementById('scroll-toggle');
        const refreshCheck = document.getElementById('refresh-toggle');
        
        const jobStatusBadge = document.getElementById('job-status');
        
        let currentOffset = 0;
        let timer = null;
        const collapsedSections = new Set();
        const TERMINAL_STATUSES = ['success', 'failed', 'canceled', 'skipped', 'manual'];

        async function refresh() {{
            try {{
                const res = await fetch(`/api/v1/deploy/jobs/${{job_id}}/trace/ui?offset=${{currentOffset}}`);
                const json = await res.json();
                if (!json.data || !json.data.lines) throw new Error("Invalid API response");

                const data = json.data;
                const status = data.status || 'unknown';
                jobStatusBadge.innerText = status.toUpperCase();

                if (TERMINAL_STATUSES.includes(status)) {{
                    if (timer) clearInterval(timer);
                    timer = null;
                    refreshCheck.checked = false;
                    const dot = document.querySelector('.dot');
                    if (dot) {{
                        dot.style.animation = 'none';
                        dot.style.opacity = '0.5';
                    }}
                    syncStatus.innerText = `Finished at ${{new Date().toLocaleTimeString()}}`;
                }} else {{
                    syncStatus.innerText = `Updated ${{new Date().toLocaleTimeString()}}`;
                }}

                if (data.lines.length === 0) return;
                currentOffset = data.next_offset;

                render(data.lines);
                
                if (scrollCheck.checked) container.scrollTop = container.scrollHeight;
            }} catch (e) {{
                syncStatus.innerText = "Sync Failed";
                console.error(e);
            }}
        }}

        window.toggleSection = (sid) => {{
            const isCollapsed = collapsedSections.has(sid);
            if (isCollapsed) {{
                collapsedSections.delete(sid);
            }} else {{
                collapsedSections.add(sid);
            }}
            
            const headers = document.querySelectorAll(`.section-header[data-sid="${{sid}}"]`);
            headers.forEach(h => h.classList.toggle('collapsed', !isCollapsed));
            
            const lines = document.querySelectorAll(`.log-line[data-parent-sid="${{sid}}"]`);
            lines.forEach(l => l.classList.toggle('hidden', !isCollapsed));
        }};

        function render(lines) {{
            let html = '';
            lines.forEach(l => {{
                const isHeader = l.is_section_header ? ' section-header section' : '';
                const isCollapsed = collapsedSections.has(l.section_id) ? ' collapsed' : '';
                const hidden = (l.section_id && collapsedSections.has(l.section_id) && !l.is_section_header) ? ' hidden' : '';

                const caret = l.is_section_header ? '<span class="caret"></span>' : '';
                const indent = l.depth * 20;

                const dataSid = l.is_section_header ? ` data-sid="${{l.section_id}}"` : '';
                const parentSid = (l.section_id && !l.is_section_header) ? ` data-parent-sid="${{l.section_id}}"` : '';
                const onclick = l.is_section_header ? ` onclick="toggleSection('${{l.section_id}}')"` : '';

                html += `<div class="log-line${{isHeader}}${{isCollapsed}}${{hidden}}"${{dataSid}}${{parentSid}}${{onclick}}><div class="cell line-num">${{l.num}}</div><div class="cell timestamp">${{l.timestamp || '&nbsp;'}}</div><div class="cell content" style="padding-left: ${{indent}}px">${{caret}}${{l.content_html}}</div></div>`;
            }});
            table.insertAdjacentHTML('beforeend', html);
        }}

        wrapCheck.addEventListener('change', () => document.body.className = wrapCheck.checked ? 'wrap' : 'no-wrap');
        refreshCheck.addEventListener('change', () => {{
            if (timer) clearInterval(timer);
            if (refreshCheck.checked) timer = setInterval(refresh, 5000);
        }});

        refresh();
        timer = setInterval(refresh, 5000);
    </script>
</body>
</html>
"""
