import requests
import configparser
import json
import sqlite3

class ApiError(Exception):
    """An API raised error"""
    def __init__(self, url, status_code, message):
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.message = message
    def __str__(self):
        return f"[ApiError {self.status_code}]: {self.message} when calling {self.url}."
    

class DependabotIssuesReader:
    def __init__(self, owner, token):
        # Read the token from the ini file
        # Generate token at https://github.com/settings/tokens
        #owner
        self.owner = owner
        # oauth tag
        self.token = token

    def github_api_get(self, url):
        # Make the request
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/vnd.github.inertia-preview+json',
        }
        response = requests.get(url, headers=headers)
        # Check the response status and parse JSON
        if response.status_code == 200:
            return response.json()
        else:
            raise ApiError(url, response.status_code, response.text)
        
    # https://docs.github.com/en/rest/repos/repos?apiVersion=2022-11-28#list-organization-repositories    
    def get_repos(self):
        url = f'https://api.github.com/orgs/{self.owner}/repos'
        # TODO parse
        repos = []
        results =  self.github_api_get(url)
        for row in results:
            # TODO skip if repo not in use
            repos.append(row["name"])
        return repos
    
    def get_all_dependabot_alerts(self):
        repos = self.get_repos()
        alerts = []
        for repo in repos:
            print("Getting Dependabot alerts from " + repo)
            alerts = alerts + self.get_dependabot_alerts(repo)
        print("Finished.")
        return alerts
    
    # https://docs.github.com/en/rest/dependabot/alerts?apiVersion=2022-11-28#list-dependabot-alerts-for-an-organization
    def get_dependabot_alerts(self, repo):
        url = f'https://api.github.com/repos/{self.owner}/{repo}/dependabot/alerts'
        alerts = []
        try:
            results = self.github_api_get(url)
            for row in results:
                advisory = row["security_advisory"]
                alert = {}
                alert["repo"] = repo
                alert["number"] = row["number"]
                # TODO handle the state in monday.com
                alert["state"] = row["state"]
                alert["html_url"] = row["html_url"]
                alert["summary"] = advisory["summary"]
                alert["description"] = advisory["description"]
                alerts.append(alert)
        except ApiError as e:
            if "Dependabot alerts are disabled for this repository." in e.message:
                print(f"Dependabot alerts are not enabled for repository: {repo}")
            else:
                raise(e)
        finally:
            return alerts


class MondayComPoster:
    def __init__(self, api_key, board_id, group_id, user_id):
        # Read the token from the ini file
        self.api_key = api_key
        self.board_id = board_id
        self.group_id = group_id
        self.user_id = user_id  
        self.api_url = "https://api.monday.com/v2"

    def mondaycom_api_post(self, query, variables):
        headers = {
            "Authorization": self.api_key
        }
        response = requests.post(url=self.api_url, json={"query": query, "variables": variables}, headers=headers)

        # Check the response status and parse JSON
        if response.status_code == 200:
            print(response)
            print(response.content)
            return response
        else:
            raise ApiError(self.api_url, response.status_code, response.text)
        
    def send_issue(self, title, description):
        query = """
            mutation ($boardId: ID!, $groupId: String!, $itemName: String!, $columnValues: JSON!) {
                create_item (
                    board_id: $boardId,
                    group_id: $groupId,
                    item_name: $itemName,
                    column_values: $columnValues
                ) {
                    id
                }
            }
        """
        variables = {
            "boardId": self.board_id,
            "groupId": self.group_id,
            "itemName": title,
            "columnValues": json.dumps({
                "status": {"label": "open"}
            })
        }

        r = self.mondaycom_api_post(query, variables)
        jr = json.loads(r.content)
        # Extract monday.com ticket ID
        item_id = jr["data"]["create_item"]["id"]
        # Post the description as an update to the item
        print(item_id)
        #query = "mutation {create_update (item_id: %s, body: \"%s\") {id}}" % (item_id, description)
        #r = self.mondaycom_api_post(query, [])

        query = "mutation ($itemId: ID!, $body: String!) {create_update (item_id: $itemId, body: $body) {id}}"
        r = self.mondaycom_api_post(query, {"itemId": item_id, "body": description})

class ProgressTracker:
    def __init__(self, dbfilePath):
        self.dbfilePath = dbfilePath
        self.conn = sqlite3.connect(dbfilePath)

    def __del__(self):
        self.conn.close()

    # Does tracking table exist?
    def table_exists(self, tableName):
        # Validate and format the table name
        # It's important to avoid using untrusted inputs directly in SQL queries
        if not tableName.isidentifier():
            raise ValueError("Invalid repo/table name")
        
        cur = self.conn.cursor()
        res = cur.execute("SELECT name FROM sqlite_master WHERE name=:tableName", {"tableName": tableName})
        return res.fetchone() is not None
    
    def create_table(self, tableName):
        self.conn.execute(f"CREATE TABLE {tableName} (number INTEGER)")

    # called when an item is sent to monday.com
    def set_done(self, repo, number):
        # Make sure table exists
        if not self.table_exists(repo):
            self.create_table(repo)
        self.conn.execute(f"INSERT INTO {repo} (number) VALUES ({number})")
        self.conn.commit()

    # Find out if an update has already been sent
    def get_done(self, repo, number):
        if not self.table_exists(repo):
            return False
        cur = self.conn.cursor()
        res = cur.execute(f"SELECT number FROM {repo} WHERE number={number}")
        return res.fetchone() is not None

class DependabotToMonday:
    def __init__(self, settingsPath):
        # Read the token from the ini file
        # Generate token at https://github.com/settings/tokens
        config = configparser.ConfigParser()
        config.read(settingsPath)
        self.gh_owner = config['github']['owner']
        # oauth tag
        self.gh_token = config['github']['token'].strip()
        self.monday_api_key = config['mondaycom']['api_key']
        self.monday_board_id = config['mondaycom']['board_id'].strip()
        self.monday_group_id = config['mondaycom']['group_id'].strip()
        self.monday_user_id = config['mondaycom']['user_id'].strip()
        self.progress = ProgressTracker(config['general']['progressdb_path'].strip())

    def create_body(self, alert):
        return "%s\n\n%s" % (alert["description"], alert["html_url"])
    
    def get_summary(self, alert):
        return "%s/%s: %s" % (alert["repo"], alert["number"], alert["summary"])
    
    def update(self):
        dp2m = DependabotIssuesReader(self.gh_owner, self.gh_token)
        mondaycom = MondayComPoster(self.monday_api_key, self.monday_board_id, self.monday_group_id, self.monday_user_id)

        alerts = dp2m.get_all_dependabot_alerts()
        for alert in alerts:
            # Skip if already sent
            if self.progress.get_done(alert["repo"], alert["number"]):
                continue
            summary = self.get_summary(alert)           
            body = self.create_body(alert)
            print("Sending: ", summary)
            mondaycom.send_issue(summary, body)
            self.progress.set_done(alert["repo"], alert["number"])


ghToMonday = DependabotToMonday('remote.settings')
ghToMonday.update()

