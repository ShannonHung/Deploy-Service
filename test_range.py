import sys
import gitlab
from app.core.config import get_settings

async def main():
    settings = get_settings()
    gl = gitlab.Gitlab(url=settings.GITLAB_URL, private_token=settings.GITLAB_TOKEN)
    project_id = settings.GITLAB_PROJECT_ID
    job_id = 13413982275

    url = f"/projects/{project_id}/jobs/{job_id}/trace"
    
    # Try fetching first 100 bytes
    try:
        response = gl.http_get(url, headers={"Range": "bytes=0-100"}, raw=True)
        print("Status:", response.status_code)
        print("Content-Range:", response.headers.get("Content-Range"))
        print("Data:", repr(response.content))
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
