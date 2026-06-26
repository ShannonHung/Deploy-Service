"""app/core/login_template.py

Minimal browser-first login page. Swagger's /token is an XHR whose Set-Cookie
the browser does not persist, so the HTML log viewers never receive the auth
cookie. This page is a normal same-origin form POST, after which the browser
holds the access_token cookie and viewer pages work by plain navigation.

`{next}` and `{error}` are filled server-side and MUST already be HTML-escaped
by the caller (they originate from user input).
"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign in</title>
    <style>
        body {{
            background: #0d1117; color: #c9d1d9; height: 100vh; margin: 0;
            display: flex; align-items: center; justify-content: center;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }}
        .card {{
            background: #161b22; border: 1px solid #30363d; border-radius: 10px;
            padding: 32px 36px; width: 320px; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
        }}
        h1 {{ font-size: 18px; margin: 0 0 4px; }}
        p.sub {{ font-size: 13px; color: #8b949e; margin: 0 0 20px; }}
        label {{ display: block; font-size: 12px; color: #8b949e; margin: 14px 0 6px; }}
        input[type=text], input[type=password] {{
            width: 100%; box-sizing: border-box; padding: 9px 10px; border-radius: 6px;
            border: 1px solid #30363d; background: #0d1117; color: #c9d1d9; font-size: 14px;
        }}
        button {{
            margin-top: 22px; width: 100%; padding: 10px; border: none; border-radius: 6px;
            background: #238636; color: #fff; font-size: 14px; font-weight: 600; cursor: pointer;
        }}
        button:hover {{ background: #2ea043; }}
        .error {{
            margin: 14px 0 0; padding: 8px 10px; border-radius: 6px;
            border: 1px solid #f85149; background: rgba(248,81,73,0.1);
            color: #ff7b72; font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Sign in</h1>
        <p class="sub">Authenticate to view command logs.</p>
        {error}
        <form method="post" action="/login">
            <input type="hidden" name="next" value="{next}">
            <label for="username">Username</label>
            <input type="text" id="username" name="username" autocomplete="username" autofocus>
            <label for="password">Password</label>
            <input type="password" id="password" name="password" autocomplete="current-password">
            <button type="submit">Sign in</button>
        </form>
    </div>
</body>
</html>
"""
