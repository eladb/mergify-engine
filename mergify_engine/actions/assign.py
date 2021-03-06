# -*- encoding: utf-8 -*-
#
#  Copyright © 2020 Mergify SAS
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

import httpx
import voluptuous

from mergify_engine import actions
from mergify_engine import context
from mergify_engine.rules import types


class AssignAction(actions.Action):
    validator = {voluptuous.Required("users", default=[]): [types.Jinja2]}

    silent_report = True

    def run(self, ctxt, rule, missing_conditions):
        wanted = set()
        for user in set(self.config["users"]):
            try:
                user = ctxt.pull_request.render_template(user)
            except context.RenderTemplateFailure as rmf:
                # NOTE: this should never happen since the template is validated when parsing the config 🤷
                return ("failed", "Invalid assignee", str(rmf))
            else:
                wanted.add(user)

        already = set((user["login"] for user in ctxt.pull["assignees"]))
        assignees = list(wanted - already)
        if assignees:
            try:
                ctxt.client.post(
                    f"issues/{ctxt.pull['number']}/assignees",
                    json={"assignees": assignees},
                )
            except httpx.HTTPClientSideError as e:  # pragma: no cover
                return (
                    None,
                    "Unable to add assignees",
                    f"GitHub error: [{e.status_code}] `{e.message}`",
                )

        return ("success", "Assignees added", ", ".join(self.config["users"]))
