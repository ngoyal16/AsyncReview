from .base import BaseProvider
from .github import GithubProvider
from .gitlab import GitlabProvider

# List of available providers
_PROVIDERS: list[type[BaseProvider]] = [
    GithubProvider,
    GitlabProvider,
]

def get_provider(url: str) -> BaseProvider:
    """Get the appropriate provider for the given URL."""
    for provider_cls in _PROVIDERS:
        provider = provider_cls()
        if provider.is_match(url):
            return provider

    raise ValueError(f"No provider found for URL: {url}")
