import socket
import asyncio
import time
import json
import uuid
import requests

from walkoff_app_sdk.app_base import AppBase


class AzureSentinel(AppBase):
    __version__ = "1.0.0"
    app_name = "Azure Sentinel"

    def __init__(self, redis, logger, console_logger=None):
        """
        Each app should have this __init__ to set up Redis and logging.
        :param redis:
        :param logger:
        :param console_logger:
        """
        super().__init__(redis, logger, console_logger)
        self.azure_url = "https://management.azure.com"

    async def authenticate(self, tenant_id, client_id, client_secret):

        self.s = requests.Session()
        auth_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        auth_data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": f"{self.azure_url}/.default",
        }
        auth_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "cache-control": "no-cache",
        }

        self.logger.debug(f"Making request to: {auth_url}")
        res = self.s.post(auth_url, data=auth_data, headers=auth_headers)

        if res.status_code != 200:
            self.logger.error("Authentication error has occurred: ", res.json())
            return {"success": False, "message": res.text}

        access_token = res.json().get("access_token")
        self.s.headers = {"Authorization": f"Bearer {access_token}", "cache-control": "no-cache"}
        return {"success": True, "message": res.text}

    async def extract_entities(self, incident_uri):

        entities_url = f"{self.azure_url}{incident_uri}/entities"
        params = {"api-version": "2019-01-01-preview"}
        self.logger.debug(f"Making request to: {entities_url}")
        res = self.s.post(entities_url, params=params)
        if res.status_code != 200:
            self.logger.error(f"Failed to get entities for {incident_uri}")

        return res.json().get("entities", [])

    async def extract_comments(self, incident_uri):

        comments_url = f"{self.azure_url}{incident_uri}/comments"
        params = {"api-version": "2020-01-01"}
        self.logger.debug(f"Making request to: {comments_url}")
        res = self.s.get(comments_url, params=params)
        if res.status_code != 200:
            self.logger.error(f"Failed to get comments for {incident_uri}")

        return res.json().get("value", [])

    async def get_incidents(self, **kwargs):

        # Get a client credential access token
        auth = await self.authenticate(
            kwargs["tenant_id"], kwargs["client_id"], kwargs["client_secret"]
        )
        if not auth["success"]:
            return {"error": auth["message"]}

        incidents_url = f"{self.azure_url}/subscriptions/{kwargs['subscription_id']}/resourceGroups/{kwargs['resource_group_name']}/providers/Microsoft.OperationalInsights/workspaces/{kwargs['workspace_name']}/providers/Microsoft.SecurityInsights/incidents"
        params = {"api-version": "2020-01-01"}

        # Add query filters if defined
        query_filter = ""
        if kwargs["status"]:
            status_filters = [
                f"properties/status eq '{x.strip()}'" for x in kwargs["status"].split(",")
            ]
            query_filter = f"( {' or '.join(status_filters)} )"
            self.logger.debug(f"Adding query filter for status: {query_filter}")

        if kwargs["last_modified"]:
            status_filters = [
                f"properties/status eq '{x.strip()}'" for x in kwargs["status"].split(",")
            ]
            query_filter = f"{query_filter} and (properties/lastModifiedTimeUtc ge {kwargs['last_modified']}Z)"
            self.logger.debug(f"Adding query filter for last_modified: {query_filter}")

        if query_filter:
            params["$filter"] = query_filter

        self.logger.info(f"Making request to: {incidents_url}")
        res = self.s.get(incidents_url, params=params)
        if res.status_code != 200:
            return res.text
        incidents = res.json()["value"]

        # Get incident entities
        if kwargs.get("get_entities", "").lower() == "true":
            for incident in incidents:
                self.logger.warning(f"Getting entities for {incident['id']}")
                incident["entities"] = await self.extract_entities(incident["id"])

        # Get incident comments
        if kwargs.get("get_comments", "").lower() == "true":
            for incident in incidents:
                self.logger.warning(f"Getting entities for {incident['id']}")
                incident["comments"] = await self.extract_comments(incident["id"])

        return json.dumps(incidents)

    async def get_incident(self, **kwargs):

        if not kwargs.get("incident_id"):
            return '{"success": false, "error": "No incident ID supplied"}'

        # Get a client credential access token
        auth = await self.authenticate(
            kwargs["tenant_id"], kwargs["client_id"], kwargs["client_secret"]
        )

        incident_url = f"{self.azure_url}/subscriptions/{kwargs['subscription_id']}/resourceGroups/{kwargs['resource_group_name']}/providers/Microsoft.OperationalInsights/workspaces/{kwargs['workspace_name']}/providers/Microsoft.SecurityInsights/incidents/{kwargs['incident_id']}"
        params = {"api-version": "2020-01-01"}

        res = self.s.get(incident_url, params=params)
        if res.status_code != 200:
            return res.text
        incident = res.json()

        # Get incident entities
        if kwargs.get("get_entities", "").lower() == "true":
            incident["entities"] = await self.extract_entities(incident["id"])

        # Get incident comments
        if kwargs.get("get_comments", "").lower() == "true":
            incident["comments"] = await self.extract_comments(incident["id"])

        return json.dumps(incident)

    async def close_incident(self, **kwargs):

        incident = json.loads(await self.get_incident(**kwargs))
        if "error" in incident:
            return json.dumps(incident)

        # Get classification and classificationReason
        close_reason = [x.strip() for x in kwargs["close_reason"].split("-")]

        close_data = {
            "etag": incident.get("etag", "").strip('"'),
            "properties": {
                "title": incident["properties"]["title"],
                "status": "Closed",
                "severity": incident["properties"]["severity"],
                "classification": close_reason[0],
                "classificationComment": kwargs["close_comment"],
            },
        }
        if len(close_reason) > 1:
            close_data["properties"]["classificationReason"] = close_reason[1]

        incident_url = f"{self.azure_url}/subscriptions/{kwargs['subscription_id']}/resourceGroups/{kwargs['resource_group_name']}/providers/Microsoft.OperationalInsights/workspaces/{kwargs['workspace_name']}/providers/Microsoft.SecurityInsights/incidents/{kwargs['incident_id']}"
        params = {"api-version": "2020-01-01"}

        res = self.s.put(incident_url, json=close_data, params=params)
        if res.status_code != 200:
            return res.text

        result = res.json()
        result["success"] = True

        return json.dumps(result)

    async def add_comment(self, **kwargs):

        # Get a client credential access token
        auth = await self.authenticate(
            kwargs["tenant_id"], kwargs["client_id"], kwargs["client_secret"]
        )
        if not auth["success"]:
            return {"error": auth["message"]}

        comment_url = f"{self.azure_url}/subscriptions/{kwargs['subscription_id']}/resourceGroups/{kwargs['resource_group_name']}/providers/Microsoft.OperationalInsights/workspaces/{kwargs['workspace_name']}/providers/Microsoft.SecurityInsights/incidents/{kwargs['incident_id']}/comments"
        params = {"api-version": "2020-01-01"}

        comment_id = str(uuid.uuid4())
        comment_data = {"properties": {"message": kwargs["comment"]}}

        res = self.s.put(f"{comment_url}/{comment_id}", json=comment_data, params=params)
        if res.status_code != 200:
            return res.text

        result = res.json()
        result["success"] = True

        return json.dumps(result)


if __name__ == "__main__":
    asyncio.run(AzureSentinel.run(), debug=True)
