#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List

import requests


API_BASE = "https://api.github.com"
README_PATH = "README.md"
START_MARKER = "<!-- TOP_LANGUAGES:START -->"
END_MARKER = "<!-- TOP_LANGUAGES:END -->"
TOP_N = 10
BAR_WIDTH = 20
LANG_COL_WIDTH = 12
REQUEST_TIMEOUT = 30
SLEEP_BETWEEN_LANGUAGE_CALLS = 0.2

EXCLUDED_LANGUAGES = {
    # Add items if you want to hide markup/build languages, for example:
    # "HTML", "CSS", "Makefile"
}


@dataclass
class Repo:
    name: str
    owner: str
    fork: bool
    archived: bool
    disabled: bool


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def github_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "top-languages-readme-updater",
        }
    )
    return session


def get_paginated(session: requests.Session, url: str, params: dict | None = None) -> Iterable[dict]:
    while url:
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError(
                f"Expected list response from {url}, got: {type(data).__name__}")
        for item in data:
            yield item

        next_url = None
        link = response.headers.get("Link", "")
        if link:
            for part in link.split(","):
                section = part.strip()
                if 'rel="next"' in section:
                    match = re.search(r"<([^>]+)>", section)
                    if match:
                        next_url = match.group(1)
                        break

        url = next_url
        params = None


def list_public_user_repos(session: requests.Session, username: str) -> List[Repo]:
    url = f"{API_BASE}/users/{username}/repos"
    params = {"per_page": 100, "type": "public", "sort": "updated"}
    repos: List[Repo] = []

    for item in get_paginated(session, url, params=params):
        repos.append(
            Repo(
                name=item["name"],
                owner=item["owner"]["login"],
                fork=bool(item.get("fork")),
                archived=bool(item.get("archived")),
                disabled=bool(item.get("disabled")),
            )
        )
    return repos


def should_include_repo(repo: Repo, username: str) -> bool:
    if repo.fork or repo.archived or repo.disabled:
        return False
    if repo.name.lower() == username.lower():
        return False
    return True


def fetch_repo_languages(session: requests.Session, owner: str, repo: str) -> Dict[str, int]:
    url = f"{API_BASE}/repos/{owner}/{repo}/languages"
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Expected dict language response for {owner}/{repo}")
    return {str(k): int(v) for k, v in data.items()}


def aggregate_languages(session: requests.Session, repos: List[Repo]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for idx, repo in enumerate(repos, start=1):
        print(f"[{idx}/{len(repos)}] Fetching languages for {repo.owner}/{repo.name}")
        try:
            lang_map = fetch_repo_languages(session, repo.owner, repo.name)
        except requests.HTTPError as exc:
            print(f"  ! Skipping {repo.name}: {exc}", file=sys.stderr)
            continue

        for language, byte_count in lang_map.items():
            if language in EXCLUDED_LANGUAGES:
                continue
            totals[language] = totals.get(language, 0) + byte_count

        time.sleep(SLEEP_BETWEEN_LANGUAGE_CALLS)

    return totals


def make_bar(percent: float, width: int = BAR_WIDTH) -> str:
    filled = round((percent / 100.0) * width)
    filled = max(0, min(width, filled))
    return ("█" * filled) + ("░" * (width - filled))


def format_line(language: str, percent: float) -> str:
    padded = language.ljust(LANG_COL_WIDTH)
    return f"{padded} [{make_bar(percent)}] {percent:.2f}%"


def render_block(language_totals: Dict[str, int]) -> str:
    grand_total = sum(language_totals.values())
    if grand_total <= 0:
        raise RuntimeError("No language data found after aggregation.")

    ranked = sorted(language_totals.items(),
                    key=lambda kv: kv[1], reverse=True)[:TOP_N]
    lines = []
    for language, byte_count in ranked:
        percent = (byte_count / grand_total) * 100.0
        lines.append(format_line(language, percent))

    return "```text\n" + "\n".join(lines) + "\n```"


def update_readme(readme_path: str, generated_block: str) -> bool:
    with open(readme_path, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = re.compile(
        re.escape(START_MARKER) + r"[\s\S]*?" + re.escape(END_MARKER),
        re.MULTILINE,
    )

    if not pattern.search(content):
        raise RuntimeError(
            f"README markers not found. Add these markers:\n{START_MARKER}\n{END_MARKER}"
        )

    replacement = f"{START_MARKER}\n{generated_block}\n{END_MARKER}"
    updated = pattern.sub(replacement, content, count=1)

    if updated == content:
        print("README is already up to date.")
        return False

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(updated)

    print("README updated.")
    return True


def main() -> int:
    token = env("GITHUB_TOKEN")
    username = env("GH_USERNAME")

    session = github_session(token)

    print(f"Listing public repositories for {username}...")
    repos = list_public_user_repos(session, username)
    repos = [repo for repo in repos if should_include_repo(repo, username)]
    print(f"Included repositories: {len(repos)}")

    if not repos:
        raise RuntimeError("No eligible repositories found.")

    language_totals = aggregate_languages(session, repos)
    block = render_block(language_totals)
    changed = update_readme(README_PATH, block)

    if changed:
        print("Top Languages section regenerated successfully.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
