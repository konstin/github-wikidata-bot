import argparse
import json

from main import Settings, query_projects, get_all_github_releases, analyse_release


def debug_version_handling(threshold: int = 50):
    with open("config.json") as config:
        github_oath_token = json.load(config)["github-oauth-token"]
    Settings.cached_session.headers.update(
        {"Authorization": "token " + github_oath_token}
    )
    projects = query_projects()[:threshold]
    for project in projects:
        github_releases = get_all_github_releases(project["repo"])
        for github_release in github_releases:
            release = analyse_release(github_release, project["projectLabel"])
            print(
                "{:10} | {:10} | {:15} | {:25} | {}".format(
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
    args = parser.parse_args()

    debug_version_handling(args.threshold)
