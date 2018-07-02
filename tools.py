#!/usr/bin/env python3
import argparse
import json
from random import sample
from typing import Iterable, Dict

from main import (
    Settings,
    analyse_release,
    get_all_github_releases,
    logger,
    query_projects,
)


def safe_sample(population: Iterable[Dict[str, str]], size: int):
    try:
        return sample(population, size)
    except ValueError:
        return population


def debug_version_handling(
    threshold: int = 50, size: int = 20, no_sampling: bool = False
):
    logger.setLevel(40)
    projects = query_projects()
    if not no_sampling:
        projects = safe_sample(projects, threshold)
    for project in projects:
        github_releases = get_all_github_releases(project["repo"])
        if not no_sampling:
            github_releases = safe_sample(github_releases, size)
        for github_release in github_releases:
            release = analyse_release(github_release, project["projectLabel"])
            print(
                "{:15} | {:10} | {:20} | {:25} | {}".format(
                    release["version"] if release else "---",
                    release["release_type"] if release else "---",
                    github_release["tag_name"],
                    repr(project["projectLabel"]),
                    github_release["name"],
                )
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", default=50, type=int)
    parser.add_argument("--maxsize", default=20, type=int)
    parser.add_argument("--no-sampling", action="store_true", default=False)
    parser.add_argument("--github-oauth-token")
    args = parser.parse_args()

    if args.github_oauth_token:
        github_oath_token = args.github_oauth_token
    else:
        with open("config.json") as config:
            github_oath_token = json.load(config)["github-oauth-token"]
    Settings.cached_session.headers.update(
        {"Authorization": "token " + github_oath_token}
    )
    debug_version_handling(args.threshold, args.maxsize, args.no_sampling)
