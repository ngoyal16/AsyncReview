import sys
import os
import unittest
from pathlib import Path

# Add repo root to path
sys.path.append(str(Path(__file__).parent.parent))

from cr.providers.factory import get_provider
from cr.providers.github import GithubProvider
from cr.providers.gitlab import GitlabProvider
from cr.config import GITLAB_TOKENS

class TestProviders(unittest.TestCase):
    def test_github_provider_match(self):
        url = "https://github.com/owner/repo/pull/123"
        provider = get_provider(url)
        self.assertIsInstance(provider, GithubProvider)

        owner, repo, number = provider.parse_url(url)
        self.assertEqual(owner, "owner")
        self.assertEqual(repo, "repo")
        self.assertEqual(number, 123)

    def test_gitlab_provider_match_public(self):
        url = "https://gitlab.com/group/project/-/merge_requests/456"
        provider = get_provider(url)
        self.assertIsInstance(provider, GitlabProvider)

        host, owner, repo, iid = provider.parse_url(url)
        self.assertEqual(host, "gitlab.com")
        self.assertEqual(owner, "group")
        self.assertEqual(repo, "project")
        self.assertEqual(iid, 456)

    def test_gitlab_provider_match_subgroup(self):
        url = "https://gitlab.com/group/subgroup/project/-/merge_requests/789"
        provider = get_provider(url)
        self.assertIsInstance(provider, GitlabProvider)

        host, owner, repo, iid = provider.parse_url(url)
        self.assertEqual(host, "gitlab.com")
        self.assertEqual(owner, "group/subgroup")
        self.assertEqual(repo, "project")
        self.assertEqual(iid, 789)

    def test_gitlab_self_hosted(self):
        # Mock config
        original_tokens = GITLAB_TOKENS.copy()
        GITLAB_TOKENS["git.corp.com"] = "token"
        try:
            url = "https://git.corp.com/org/repo/-/merge_requests/1"
            provider = get_provider(url)
            self.assertIsInstance(provider, GitlabProvider)

            host, owner, repo, iid = provider.parse_url(url)
            self.assertEqual(host, "git.corp.com")
            self.assertEqual(owner, "org")
            self.assertEqual(repo, "repo")
            self.assertEqual(iid, 1)
        finally:
            # Restore
            GITLAB_TOKENS.clear()
            GITLAB_TOKENS.update(original_tokens)

    def test_gitlab_self_hosted_fallback(self):
        # Even if not in tokens, if it follows the pattern it should match?
        # The logic says: if /-/merge_requests/ in url -> match
        url = "https://unknown.host.com/org/repo/-/merge_requests/1"
        provider = get_provider(url)
        self.assertIsInstance(provider, GitlabProvider)

        host, owner, repo, iid = provider.parse_url(url)
        self.assertEqual(host, "unknown.host.com")
        self.assertEqual(owner, "org")
        self.assertEqual(repo, "repo")
        self.assertEqual(iid, 1)

if __name__ == '__main__':
    unittest.main()
