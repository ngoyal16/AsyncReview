from abc import ABC, abstractmethod
from ..diff_types import PRInfo, FileContents

class BaseProvider(ABC):
    """Abstract base class for code providers (GitHub, GitLab, etc)."""

    @abstractmethod
    def is_match(self, url: str) -> bool:
        """Check if the provider can handle this URL."""
        pass

    @abstractmethod
    async def load_pr(self, url: str) -> PRInfo:
        """Load PR metadata from the provider."""
        pass

    @abstractmethod
    async def get_file_contents(
        self, pr_info: PRInfo, path: str
    ) -> tuple[FileContents | None, FileContents | None]:
        """Get old and new file contents for a file in a PR.

        Args:
            pr_info: The PR metadata object
            path: File path within the repo

        Returns:
            Tuple of (old_file, new_file) - either can be None
        """
        pass
