from typing import Optional
from .diff_types import PRInfo, FileContents
from .providers.factory import get_provider

# Cache: review_id -> {info: PRInfo, url: str}
_pr_cache: dict[str, dict] = {}

async def load_pr(url: str) -> PRInfo:
    """Load PR metadata from the appropriate provider."""
    provider = get_provider(url)
    pr_info = await provider.load_pr(url)
    _pr_cache[pr_info.review_id] = {"info": pr_info, "url": url}
    return pr_info

def get_cached_pr(review_id: str) -> PRInfo | None:
    """Get a cached PR by review ID."""
    entry = _pr_cache.get(review_id)
    return entry["info"] if entry else None

async def get_file_contents(review_id: str, path: str) -> tuple[FileContents | None, FileContents | None]:
    """Get file contents using the cached PR context."""
    entry = _pr_cache.get(review_id)
    if not entry:
        raise ValueError(f"Review {review_id} not found")

    pr_info = entry["info"]
    url = entry["url"]
    provider = get_provider(url)
    return await provider.get_file_contents(pr_info, path)
