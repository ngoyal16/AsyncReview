import re
import uuid
import urllib.parse
from datetime import datetime
import httpx

from ..config import GITLAB_TOKEN, GITLAB_TOKENS
from ..diff_types import PRInfo, FileContents
from .base import BaseProvider


class GitlabProvider(BaseProvider):
    """GitLab provider implementation (SaaS and Self-Hosted)."""

    def is_match(self, url: str) -> bool:
        # Check for standard GitLab URL pattern
        if "/-/merge_requests/" in url:
            return True
        # Check against configured self-hosted domains
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname in GITLAB_TOKENS:
            return True
        if parsed.hostname == "gitlab.com":
            return True
        return False

    def parse_url(self, url: str) -> tuple[str, str, str, int]:
        """Parse a GitLab MR URL into (host, owner, repo, iid)."""
        # Expected format: https://gitlab.com/group/project/-/merge_requests/123
        # or https://git.corp.com/group/subgroup/project/-/merge_requests/123

        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "gitlab.com"

        # Regex to capture path parts
        # Path: /group/project/-/merge_requests/123
        pattern = r"^/(.+)/-/merge_requests/(\d+)"
        match = re.search(pattern, parsed.path)
        if not match:
            raise ValueError(f"Invalid GitLab MR URL: {url}")

        full_path = match.group(1)
        iid = int(match.group(2))

        # Split full_path into owner (namespace) and repo (project)
        # In GitLab, the "project ID" usually covers the full path, so we can treat
        # the last segment as repo and the rest as owner, or just keep full path.
        # For simplicity and compatibility with PRInfo, let's say:
        # owner = namespace (e.g., "group/subgroup")
        # repo = project_name (e.g., "my-app")

        if "/" in full_path:
            parts = full_path.rsplit("/", 1)
            owner = parts[0]
            repo = parts[1]
        else:
            owner = ""
            repo = full_path

        return host, owner, repo, iid

    def _get_api_base(self, host: str) -> str:
        """Get API base URL for the host."""
        if host == "gitlab.com":
            return "https://gitlab.com/api/v4"
        return f"https://{host}/api/v4"

    def _get_headers(self, host: str) -> dict[str, str]:
        """Get HTTP headers for GitLab API requests."""
        token = GITLAB_TOKENS.get(host)
        if not token and host == "gitlab.com":
            token = GITLAB_TOKEN

        headers = {
            "User-Agent": "cr-review-tool",
        }
        if token:
            headers["PRIVATE-TOKEN"] = token
        return headers

    async def load_pr(self, url: str) -> PRInfo:
        host, owner, repo, iid = self.parse_url(url)
        review_id = str(uuid.uuid4())[:8]

        # GitLab API requires URL-encoded project path (namespace/project)
        project_path = f"{owner}/{repo}" if owner else repo
        project_id = urllib.parse.quote(project_path, safe="")

        api_base = self._get_api_base(host)
        headers = self._get_headers(host)

        async with httpx.AsyncClient() as client:
            # Fetch MR metadata + Changes (single call in GitLab!)
            mr_resp = await client.get(
                f"{api_base}/projects/{project_id}/merge_requests/{iid}/changes",
                headers=headers,
                timeout=30.0,
            )
            mr_resp.raise_for_status()
            mr_data = mr_resp.json()

            # Fetch commits
            commits_resp = await client.get(
                f"{api_base}/projects/{project_id}/merge_requests/{iid}/commits",
                headers=headers,
                params={"per_page": 100},
                timeout=30.0,
            )
            commits_list = []
            if commits_resp.status_code == 200:
                commits_data = commits_resp.json()
                commits_list = [
                    {
                        "sha": c["id"],
                        "message": c["message"],
                        "author": {
                            "name": c["author_name"],
                            "date": c["created_at"],
                            "login": c.get("author_email"), # GitLab commits might not have login
                            "avatar_url": None, # Hard to get without extra calls
                        },
                        "html_url": c.get("web_url", ""),
                    }
                    for c in commits_data
                ]

            # Fetch notes (comments)
            notes_resp = await client.get(
                f"{api_base}/projects/{project_id}/merge_requests/{iid}/notes",
                headers=headers,
                params={"per_page": 100, "sort": "asc", "order_by": "created_at"},
                timeout=30.0,
            )
            comments_list = []
            if notes_resp.status_code == 200:
                 notes_data = notes_resp.json()
                 # Filter only user comments (not system events) if needed,
                 # but GitLab returns all. We can filter by `system: false`.
                 comments_list = [
                     {
                         "id": n["id"],
                         "user": {
                             "login": n["author"]["username"],
                             "avatar_url": n["author"]["avatar_url"],
                         },
                         "body": n["body"],
                         "created_at": n["created_at"],
                         "html_url": f"{url}#note_{n['id']}", # Approximate URL
                     }
                     for n in notes_data
                     if not n.get("system", False)
                 ]

        files = [
            {
                 "path": f["new_path"],
                 "status": "new" if f["new_file"] else ("deleted" if f["deleted_file"] else "modified"),
                 "additions": 0, # GitLab diff doesn't provide per-file additions/deletions easily in 'changes'
                 "deletions": 0,
                 "patch": f["diff"],
            }
            for f in mr_data.get("changes", [])
        ]

        # Store host in repo field or somewhere else?
        # PRInfo expects repo as str. We can store "owner" and "repo" as usual.
        # But for file fetching, we need to know the host later.
        # We can encode host in the "owner" field? e.g. "gitlab.com/owner"
        # Or we rely on the provider implementation to re-parse the URL or store context?
        # The 'get_file_contents' takes 'PRInfo'.
        # But 'PRInfo' doesn't have a 'host' field.
        # Hack: Store host in 'owner' as 'host:owner' or just rely on 'base_ref' if it has full URL?
        # Better: Add 'host' to PRInfo (requires modifying types) OR
        # Encode host in owner.

        # Let's check PRInfo definition in types.
        # It has "review_id, owner, repo".
        # We can store owner as "host/owner".

        stored_owner = f"{host}/{owner}" if owner else host

        pr_info = PRInfo(
            review_id=review_id,
            owner=stored_owner, # HACK: Storing host in owner to retrieve it later
            repo=repo,
            number=iid,
            title=mr_data.get("title", ""),
            body=mr_data.get("description") or "",
            base_sha=mr_data["diff_refs"]["base_sha"],
            head_sha=mr_data["diff_refs"]["head_sha"],
            files=files,
            created_at=datetime.now(),
            user={"login": mr_data["author"]["username"], "avatar_url": mr_data["author"]["avatar_url"]},
            state=mr_data.get("state", "opened"),
            draft=mr_data.get("work_in_progress", False) or mr_data.get("draft", False),
            head_ref=mr_data["source_branch"],
            base_ref=mr_data["target_branch"],
            commits=len(commits_list),
            additions=0, # Not readily available
            deletions=0,
            changed_files=len(files),
            commits_list=commits_list,
            comments=comments_list,
        )

        return pr_info

    async def get_file_contents(
        self, pr_info: PRInfo, path: str
    ) -> tuple[FileContents | None, FileContents | None]:
        # Extract host from stored owner (HACK)
        if "/" in pr_info.owner:
            host, owner_part = pr_info.owner.split("/", 1)
            # Check if first part looks like a host (contains dot)
            if "." not in host:
                # Fallback for GitHub or misformatted
                host = "gitlab.com"
                owner = pr_info.owner
            else:
                owner = owner_part
        else:
            host = pr_info.owner if "." in pr_info.owner else "gitlab.com"
            owner = ""

        repo = pr_info.repo
        project_path = f"{owner}/{repo}" if owner else repo
        project_id = urllib.parse.quote(project_path, safe="")

        api_base = self._get_api_base(host)
        headers = self._get_headers(host)

        base_sha = pr_info.base_sha
        head_sha = pr_info.head_sha

        async with httpx.AsyncClient() as client:
            old_file = None
            new_file = None

            file_path_encoded = urllib.parse.quote(path, safe="")

            # Fetch base version
            try:
                base_resp = await client.get(
                    f"{api_base}/projects/{project_id}/repository/files/{file_path_encoded}/raw",
                    headers=headers,
                    params={"ref": base_sha},
                    timeout=30.0,
                )
                if base_resp.status_code == 200:
                    old_file = FileContents(
                        name=path,
                        contents=base_resp.text,
                        cache_key=f"{host}/{project_path}/{base_sha}/{path}",
                    )
            except httpx.HTTPStatusError:
                pass

            # Fetch head version
            try:
                head_resp = await client.get(
                    f"{api_base}/projects/{project_id}/repository/files/{file_path_encoded}/raw",
                    headers=headers,
                    params={"ref": head_sha},
                    timeout=30.0,
                )
                if head_resp.status_code == 200:
                    new_file = FileContents(
                        name=path,
                        contents=head_resp.text,
                        cache_key=f"{host}/{project_path}/{head_sha}/{path}",
                    )
            except httpx.HTTPStatusError:
                pass

        return old_file, new_file
