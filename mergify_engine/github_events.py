# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from datadog import statsd

from mergify_engine import config
from mergify_engine import logs
from mergify_engine import utils
from mergify_engine import worker
from mergify_engine.clients import github


LOG = logs.getLogger(__name__)


def get_ignore_reason(event_type, data):
    if "installation" not in data:
        return "ignored, no installation found"

    elif "repository" not in data:
        return "ignored, no repository found"

    elif event_type in ["installation", "installation_repositories"]:
        return "ignored (action %s)" % data["action"]

    elif event_type in ["push"] and not data["ref"].startswith("refs/heads/"):
        return "ignored (push on %s)" % data["ref"]

    elif event_type == "status" and data["state"] == "pending":
        return "ignored (state pending)"

    elif event_type == "check_suite" and data["action"] != "rerequested":
        return "ignored (check_suite/%s)" % data["action"]

    elif (
        event_type in ["check_run", "check_suite"]
        and data[event_type]["app"]["id"] == config.INTEGRATION_ID
        and data["action"] != "rerequested"
    ):
        return "ignored (mergify %s)" % event_type

    elif event_type == "issue_comment" and data["action"] != "created":
        return "ignored (comment have been %s)" % data["action"]

    elif (
        event_type == "issue_comment"
        and "@mergify " not in data["comment"]["body"].lower()
        and "@mergifyio " not in data["comment"]["body"].lower()
    ):
        return "ignored (comment is not for mergify)"

    if data["repository"].get("archived"):  # pragma: no cover
        return "ignored (repository archived)"

    elif event_type not in [
        "issue_comment",
        "pull_request",
        "pull_request_review",
        "push",
        "status",
        "check_suite",
        "check_run",
        "refresh",
    ]:
        return "ignored (unexpected event_type)"


def meter_event(event_type, data):
    tags = [f"event_type:{event_type}"]

    if "action" in data:
        tags.append(f"action:{data['action']}")

    if (
        event_type == "pull_request"
        and data["action"] == "closed"
        and data["pull_request"]["merged"]
    ):
        if data["pull_request"]["merged_by"] and data["pull_request"]["merged_by"][
            "login"
        ] in ["mergify[bot]", "mergify-test[bot]"]:
            tags.append("by_mergify")

    statsd.increment("github.events", tags=tags)


def _extract_source_data(event_type, data):
    slim_data = {"sender": data["sender"]}

    # To extract pull request numbers
    if event_type == "status":
        slim_data["sha"] = data["sha"]
    elif event_type in ("refresh", "push") and "ref" in data:
        slim_data["ref"] = data["ref"]
    elif event_type in ("check_suite", "check_run"):
        slim_data[event_type] = {
            "head_sha": data[event_type]["head_sha"],
            "pull_requests": [
                {
                    "number": p["number"],
                    "base": {"repo": {"url": p["base"]["repo"]["url"]}},
                }
                for p in data[event_type]["pull_requests"]
            ],
        }

    # For pull_request opened/synchronise/closed
    # and refresh event
    for attr in ("action", "after", "before"):
        if attr in data:
            slim_data[attr] = data[attr]

    # For commands runner
    if event_type == "issue_comment":
        slim_data["comment"] = data["comment"]
    return slim_data


async def job_filter_and_dispatch(event_type, event_id, data):
    # TODO(sileht): is statsd async ?
    meter_event(event_type, data)

    if "installation" in data:
        installation_id = data["installation"]["id"]
    else:
        installation_id = "<unknown>"

    if "repository" in data:
        owner = data["repository"]["owner"]["login"]
        repo = data["repository"]["name"]
    else:
        owner = "<unknown>"
        repo = "<unknown>"

    reason = get_ignore_reason(event_type, data)
    if reason:
        msg_action = reason
    else:
        msg_action = "pushed to worker"
        source_data = _extract_source_data(event_type, data)

        if "pull_request" in data:
            pull_number = data["pull_request"]["number"]
        elif event_type == "issue_comment":
            pull_number = data["issue"]["number"]
        else:
            pull_number = None

        redis = await utils.get_aredis_for_stream()
        await worker.push(
            redis, installation_id, owner, repo, pull_number, event_type, source_data,
        )

    LOG.info(
        "GithubApp event %s",
        msg_action,
        event_type=event_type,
        event_id=event_id,
        install_id=installation_id,
        sender=data["sender"]["login"],
        gh_owner=owner,
        gh_repo=repo,
    )


def _get_github_pulls_from_sha(client, sha):
    for pull in client.items("pulls"):
        if pull["head"]["sha"] == sha:
            return [pull["number"]]
    return []


def extract_pull_numbers_from_event(installation, owner, repo, event_type, data):
    with github.get_client(owner, repo, installation) as client:
        if event_type == "refresh":
            if "ref" in data:
                branch = data["ref"][11:]  # refs/heads/
                return [p["number"] for p in client.items("pulls", base=branch)]
            else:
                return [p["number"] for p in client.items("pulls")]
        elif event_type == "push":
            branch = data["ref"][11:]  # refs/heads/
            return [p["number"] for p in client.items("pulls", base=branch)]
        elif event_type == "status":
            return _get_github_pulls_from_sha(client, data["sha"])
        elif event_type in ["check_suite", "check_run"]:
            # NOTE(sileht): This list may contains Pull Request from another org/user fork...
            base_repo_url = str(client.base_url)[:-1]
            pulls = data[event_type]["pull_requests"]
            # TODO(sileht): remove `"base" in p and`
            # Due to MERGIFY-ENGINE-1JZ, we have to temporary ignore pull with base missing
            pulls = [
                p["number"]
                for p in pulls
                if "base" in p and p["base"]["repo"]["url"] == base_repo_url
            ]
            if not pulls:
                sha = data[event_type]["head_sha"]
                pulls = _get_github_pulls_from_sha(client, sha)
            return pulls

    return []
