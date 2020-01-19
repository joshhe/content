import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

""" IMPORTS """

import json
import requests
from urllib.parse import urlencode, urlparse, parse_qs
from datetime import datetime

# Disable insecure warnings
requests.packages.urllib3.disable_warnings()

""" GLOBALS/PARAMS """

# Service base URL
NETLOC = "graph.microsoft.com"

INTEGRATION_NAME = "MsGraphFiles"

APP_NAME = "ms-graph-files"

RESPONSE_KEYS_DICTIONARY = {
    "@odata.context": "OdataContext",
    "@microsoft.graph.downloadUrl": "DownloadUrl",
    "id": "ID",
    "@odata.nextLink": "OdataNextLink",
}

EXCLUDE_LIST = ["eTag", "cTag", "quota"]


def parse_key_to_context(obj):
    """Parse graph api data as received from Microsoft Graph API into Demisto's conventions

    Args:
        item object: a dictionary containing the item data

    Returns:
        A Camel Cased dictionary with the relevant fields.
        groups_readable: for the human readable
        groups_outputs: for the entry context
    """
    parsed_obj = {}
    for key, value in obj.items():
        if key in EXCLUDE_LIST:
            continue
        new_key = RESPONSE_KEYS_DICTIONARY.get(key, key)
        parsed_obj[new_key] = value
        if type(value) == dict:
            parsed_obj[new_key] = parse_key_to_context(value)

    under_score_obj = createContext(parsed_obj, keyTransform=camel_case_to_underscore)
    context_entry = createContext(under_score_obj, keyTransform=string_to_context_key)

    if "Id" in list(context_entry.keys()):
        context_entry["ID"] = context_entry["Id"]
        del context_entry["Id"]
    if "CreatedBy" in list(context_entry.keys()):
        context_entry["CreatedBy"] = remove_identity_key(context_entry["CreatedBy"])
    if "LastModifiedBy" in list(context_entry.keys()):
        context_entry["LastModifiedBy"] = remove_identity_key(
            context_entry["LastModifiedBy"]
        )
    return context_entry


def remove_identity_key(source):
    """
    this function removes identity key (application, device or user) from LastModifiedBy and CreatedBy keys and
    convert it to "type" key.
    :param source: LastModifiedBy and CreatedBy dictionaries
    :return: camel case dictionary with identity key as type.
    """
    if not isinstance(source, dict):
        LOG("Input is not dictionary. Exist function.")
        return source

    dict_keys = list(source.keys())
    if len(dict_keys) != 1:
        demisto.log("Got more then one identity creator. Exit function")
        return source

    identity_key = dict_keys[0]
    new_source = {}
    if source[identity_key].get("ID"):
        new_source["ID"] = source[identity_key].get("ID")

    new_source["DisplayName"] = source[identity_key].get("DisplayName")
    new_source["Type"] = identity_key

    return new_source


def epoch_seconds() -> int:
    """
    Return the number of seconds for return current date.
    """
    return int((datetime.utcnow() - datetime.utcfromtimestamp(0)).total_seconds())


def get_encrypted(content: str, key: str) -> str:
    """
    Args:
        content (str): content to encrypt. For a request to Demistobot for a new access token, content should be
            the tenant id
        key (str): encryption key from Demistobot
    Returns:
        encrypted timestamp:content
    """

    def create_nonce() -> bytes:
        return os.urandom(12)

    def encrypt(string: str, enc_key: str) -> bytes:
        """
        Args:
            enc_key (str):
            string (str):
        Returns:
            bytes:
        """
        # String to bytes
        enc_key = base64.b64decode(enc_key)
        # Create key
        aes_gcm = AESGCM(enc_key)
        # Create nonce
        nonce = create_nonce()
        # Create ciphered data
        data = string.encode()
        ct_ = aes_gcm.encrypt(nonce, data, None)
        return base64.b64encode(nonce + ct_)

    now = epoch_seconds()
    encrypted = encrypt(f"{now}:{content}", key).decode("utf-8")
    return encrypted

def url_validation(url):
    # test if netloc

    # test if exits $skiptoken
    url_parameters = parse_qs(url.query)
    if not url_parameters.get("$skiptoken") or not url_parameters["$skiptoken"]:
        raise DemistoException(
            f"Url: {url} is not valid. Please provide another one. missing $skiptoken"
        )
    return url


class Client(BaseClient):
    """
    Client will implement the service API, should not contain Demisto logic.
    Should do requests and return data
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        auth_and_token_url = demisto.params().get("auth_id").split("@")
        if len(auth_and_token_url) != 2:
            token_retrieval_url = 'https://oproxy.demisto.ninja/obtain-token'  # guardrails-disable-line
        else:
            token_retrieval_url = auth_and_token_url[1]
        self.base_url = kwargs["base_url"]
        self.auth_id = auth_and_token_url[0]
        self.tenant_id = demisto.params().get("tenant_id")
        self.enc_key = demisto.params().get("enc_key")
        self.host = demisto.params().get("host")
        self.auto_url = token_retrieval_url
        self.access_token = self.get_access_token()
        self.headers = {"Authorization": f"Bearer {self.access_token}"}



    def http_call(self, *args, **kwargs):
        demisto.log(f"Sending: ")
        res = self._http_request(*args, **kwargs)
        if "status_code" in res and res.status_code == 401:
            self.access_token = self.get_access_token()
            res = self._http_request(*args, **kwargs)
        return res

    def return_valid_access_token_if_exist_in_context(self):
        integration_context = demisto.getIntegrationContext()
        access_token = integration_context.get("access_token")
        valid_until = integration_context.get("valid_until")
        if access_token and valid_until:
            if epoch_seconds() < valid_until:
                return access_token

    def return_token_and_save_it_in_context(self, access_token_response):
        access_token = access_token_response.get("access_token")

        if not access_token:
            return demisto.error("Access Token returned empty")
        expires_in = access_token_response.get("expires_in", 3595)
        time_buffer = 5  # seconds by which to shorten the validity period
        if expires_in - time_buffer > 0:
            # err on the side of caution with a slightly shorter access token validity period
            expires_in = expires_in - time_buffer

        demisto.setIntegrationContext(
            {"access_token": access_token, "valid_until": epoch_seconds() + expires_in}
        )
        return access_token

    def get_access_token(self):
        context_access_token = self.return_valid_access_token_if_exist_in_context()
        if context_access_token:
            return context_access_token

        body = json.dumps(
            {
                "app_name": APP_NAME,
                "registration_id": self.auth_id,
                "encrypted_token": get_encrypted(self.tenant_id, self.enc_key),
            }
        )
        try:
            access_token_res = self._http_request(
                method="POST",
                full_url=self.auto_url,
                data=body,
                headers={"Accept": "application/json"},
                url_suffix="",
            )
        except DemistoException as error:
            title = "Error in authentication. Try checking the credentials you entered."
            raise DemistoException(title, error)
        else:
            return self.return_token_and_save_it_in_context(access_token_res)

    def list_tenant_sites(self):
        url = "https://graph.microsoft.com/v1.0/sites"
        query_string = {"search": "*"}
        return self.http_call(
            method="GET",
            full_url=url,
            headers=self.headers,
            url_suffix="",
            params=query_string,
        )

    def list_drives_in_site(self, site_id=None, limit=None, next_page_url=None):
        if not any([site_id, next_page_url]):
            raise DemistoException(
                "Please pass at least one argument to this command: \n"
                "site_id: if you want to get all sites.\n"
                "limit: if you want to limit the number of command's results. \n"
                "next_page_url: if you have used the limit argument."
            )

        if limit:
            params = urlencode({"$top": limit})
        else:
            params = ""

        if next_page_url:
            url = url_validation(next_page_url)
        else:
            url = f"{self.base_url}/sites/{site_id}/drives"

        return self.http_call(
            "GET", full_url=url, params=params, headers=self.headers, url_suffix=""
        )

    def list_drive_children(
        self, object_type, object_type_id, item_id=None, limit=None, next_page_url=None
    ):
        if next_page_url:
            url = url_validation(next_page_url)
        else:
            if not item_id:
                item_id = "root"
            if object_type == "drives":
                url = f"{object_type}/{object_type_id}/items/{item_id}/children"

            elif object_type in ["groups", "sites", "users"]:
                url = f"{object_type}/{object_type_id}/drive/items/{item_id}/children"

            url = self.base_url + f"/{url}"

        if limit:
            params = urlencode({"$top": limit})
        else:
            params = ""

        return self.http_call(
            method="GET",
            full_url=url,
            url_suffix="",
            params=params,
            headers=self.headers,
        )

    def replace_existing_file(self, object_type, object_type_id, item_id, entry_id):

        file_path = demisto.getFilePath(entry_id).get("path", None)
        if not file_path:
            raise DemistoException(
                f"Could not find file path to the next entry id: {entry_id}. \n"
                f"Please provide another one."
            )
        if object_type == "drives":
            url = f"{object_type}/{object_type_id}/items/{item_id}/content"

        elif object_type in ["groups", "sites", "users"]:
            url = f"{object_type}/{object_type_id}/drive/items/{item_id}/content"

        # send request
        url = self.base_url + f"/{url}"
        with open(file_path, "rb") as file:
            self.headers["Content-Type"] = "application/octet-stream"
            return self.http_call(
                "PUT", full_url=url, data=file, headers=self.headers, url_suffix=""
            )

    def delete_file(self, object_type, object_type_id, item_id):
        if object_type == "drives":
            url = f"{object_type}/{object_type_id}/items/{item_id}"

        elif object_type in ["groups", "sites", "users"]:
            url = f"{object_type}/{object_type_id}/drive/items/{item_id}"

        # send request
        url = self.base_url + f"/{url}"
        self.headers[
            "Content-Type"
        ] = "text/plain"  # request returned empty and can not decoded to json
        if "" == self.http_call(
            "DELETE",
            full_url=url,
            headers=self.headers,
            url_suffix="",
            resp_type="text",
        ):
            # resp_type='text' returned empty and can not decoded to json
            return f"Item was deleted successfully"

    def upload_new_file(
        self, object_type, object_type_id, parent_id, file_name, entry_id
    ):
        """
        this function upload new file to a selected folder(parent_id)
        :param object_type: drive/ group/ me/ site/ users
        :param object_type_id: the selected object type id.
        :param parent_id:
        :param file_name:
        :param entry_id:
        :return:
        """
        file_path = demisto.getFilePath(entry_id).get("path")

        if "drives" == object_type:
            url = f"{object_type}/{object_type_id}/items/{parent_id}:/{file_name}:/content"

        elif object_type in ["groups", "users", "sites"]:
            url = f"{object_type}/{object_type_id}/drive/items/{parent_id}:/{file_name}:/content"
            # for sites, groups, users
        url = self.base_url + f"/{url}"
        with open(file_path, "rb") as file:
            self.headers["Content-Type"] = "application/octet-stream"
            return self.http_call(
                "PUT", full_url=url, headers=self.headers, url_suffix="", data=file
            )

    def download_file(self, object_type, object_type_id, item_id):
        if object_type == "drives":
            url = f"{object_type}/{object_type_id}/items/{item_id}/content"

        elif object_type in ["groups", "sites", "users"]:
            url = f"{object_type}/{object_type_id}/drive/items/{item_id}/content"

        # send request
        url = self.base_url + f"/{url}"
        res = self.http_call(
            "GET", full_url=url, headers=self.headers, url_suffix="", resp_type=""
        )  # it needs
        # resp_type='' to stay empty because if response type is empty http_requests returned the raw response.
        # I need it because graph api returnes an response as text without content so res.text fails
        return res

    def create_new_folder(self, object_type, object_type_id, parent_id, folder_name):
        if object_type == "drives":
            url = f"{object_type}/{object_type_id}/items/{parent_id}/children"

        elif object_type in ["groups", "sites", "users"]:
            url = f"{object_type}/{object_type_id}/drive/items/{parent_id}/children"

        # send request
        url = self.base_url + f"/{url}"

        payload = {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "rename",
        }
        self.headers["Content-Type"] = "application/json"
        demisto.log(f"sending POST to {url}, with the next payload: {payload}")
        return self.http_call(
            "POST", full_url=url, json_data=payload, headers=self.headers, url_suffix=""
        )


def download_file_command(client, args):
    object_type = args.get("object_type")
    object_type_id = args.get("object_type_id")
    item_id = args.get("item_id")

    result = client.download_file(
        object_type=object_type, object_type_id=object_type_id, item_id=item_id
    )
    stored_img = fileResult(item_id, result.content)
    return stored_img


def list_drive_children_command(client, args):
    object_type = args.get("object_type")
    object_type_id = args.get("object_type_id")
    item_id = args.get("item_id")
    limit = args.get("limit")
    next_page_url = args.get("next_page_url")

    if not item_id:
        item_id = "root"

    result = client.list_drive_children(
        object_type=object_type,
        object_type_id=object_type_id,
        item_id=item_id,
        limit=limit,
        next_page_url=next_page_url,
    )

    title = f"{INTEGRATION_NAME} - drivesItems information:"

    parsed_drive_items = [parse_key_to_context(item) for item in result["value"]]
    human_readable = tableToMarkdown(
        title, parsed_drive_items, headerTransform=pascalToSpace
    )

    drive_items_outputs = {
        "OdataContext": result["@odata.context"],
        "Value": parsed_drive_items,
    }
    context = {
        f"{INTEGRATION_NAME}.ListChildren(val.ItemID == obj.ItemID)": {
            "ParentID": item_id,
            "Children": drive_items_outputs,
        }
    }

    return (human_readable, context, result)


def list_tenant_sites_command(client, args):
    result = client.list_tenant_sites()

    parsed_sites_items = [parse_key_to_context(item) for item in result["value"]]

    context_entry = {
        "OdataContext": result.get("@odata.context"),
        "Value": parsed_sites_items,
    }
    context = {f"{INTEGRATION_NAME}.ListSites(val.ID === obj.ID)": context_entry}

    title = "List Sites:"
    human_readable = tableToMarkdown(
        title, parsed_sites_items, headerTransform=pascalToSpace
    )

    return (human_readable, context, result)


def list_drives_in_site_command(client, args):
    site_id = args.get("site_id")
    limit = args.get("limit")
    next_page_url = args.get("next_page_url")

    if next_page_url:
        url_validation(next_page_url)

    result = client.list_drives_in_site(
        site_id=site_id, limit=limit, next_page_url=next_page_url
    )
    parsed_drive_items = [parse_key_to_context(item) for item in result["value"]]

    context_entry = {
        "OdataContext": result.get("@odata.context", None),
        "Value": parsed_drive_items,
    }

    title = f"{INTEGRATION_NAME} - Drives information:"
    # Creating human readable for War room
    human_readable = tableToMarkdown(
        title, parsed_drive_items, headerTransform=pascalToSpace
    )

    # context == output
    context = {f"{INTEGRATION_NAME}.ListDrives(val.ID === obj.ID)": context_entry}

    return (human_readable, context, result)


def replace_an_existing_file_command(client, args):
    object_type = args.get("object_type")
    item_id = args.get("item_id")
    entry_id = args.get("entry_id")
    object_type_id = args.get("object_type_id")

    result = client.replace_existing_file(
        object_type, object_type_id, item_id, entry_id
    )
    context_entry = parse_key_to_context(result)

    title = f"{INTEGRATION_NAME} - File information:"
    # Creating human readable for War room
    human_readable = tableToMarkdown(
        title, context_entry, headerTransform=pascalToSpace
    )

    # context == output
    context = {f"{INTEGRATION_NAME}.ReplacedFiles(val.ID === obj.ID)": context_entry}

    return (human_readable, context, result)


def upload_new_file_command(client, args):
    object_type = args.get("object_type")
    object_type_id = args.get("object_type_id")
    parent_id = args.get("parent_id")
    file_name = args.get("file_name")
    entry_id = args.get("entry_id")

    result = client.upload_new_file(
        object_type, object_type_id, parent_id, file_name, entry_id
    )
    context_entry = parse_key_to_context(result)
    title = f"{INTEGRATION_NAME} - File information:"
    # Creating human readable for War room
    human_readable = tableToMarkdown(title, context_entry)

    # context == output
    context = {f"{INTEGRATION_NAME}.UploadedFiles(val.ID === obj.ID)": context_entry}

    return (human_readable, context, result)


def create_new_folder_command(client, args):
    object_type = args.get("object_type")
    parent_id = args.get("parent_id")
    folder_name = args.get("folder_name")
    object_type_id = args.get("object_type_id")

    result = client.create_new_folder(
        object_type, object_type_id, parent_id, folder_name
    )

    context_entry = parse_key_to_context(result)

    title = f"{INTEGRATION_NAME} - Folder information:"
    # Creating human readable for War room
    human_readable = tableToMarkdown(
        title, context_entry, headerTransform=pascalToSpace
    )

    # context == output
    context = {f"{INTEGRATION_NAME}.CreatedFolders(val.ID === obj.ID)": context_entry}

    return (human_readable, context, result)


def delete_file_command(client, args):
    object_type = args.get("object_type")
    item_id = args.get("item_id")
    object_type_id = args.get("object_type_id")

    text = client.delete_file(object_type, object_type_id, item_id)

    context_entry = text

    title = f"{INTEGRATION_NAME} - File information:"
    # Creating human readable for War room
    human_readable = tableToMarkdown(title, context_entry, headers=item_id)

    return (human_readable, text)  # == raw response


def main():
    # Should we use SSL
    verify_certificate = not demisto.params().get("insecure", False)
    proxy = demisto.params().get("proxy", False)
    base_url = demisto.params().get("host", False)
    LOG(f"Command being called is {demisto.command()}")
    try:
        # client = Client(BASE_URL, proxy=proxy, verify=verify_certificate)
        client = Client(
            base_url=base_url + "/v1.0",
            verify=verify_certificate,
            proxy=proxy,
            ok_codes=(200, 204, 201),
        )
        if demisto.command() == "test-module":
            # This is the call made when pressing the integration Test button.
            result = module_test(client)
            demisto.results(result)

        elif demisto.command() == "msgraph-delete-file":
            readable_output, raw_response = delete_file_command(client, demisto.args())
            return_outputs(readable_output=readable_output)
        elif demisto.command() == "msgraph-download-file":
            demisto.results(
                download_file_command(client, demisto.args())
            )  # it has to be demisto.results instead
            # of return_outputs. because fileResult contains 'content': '' and if that key is empty return_outputs
            # returns error
        elif demisto.command() == "msgraph-list-tenant-sites":
            return_outputs(*list_tenant_sites_command(client, demisto.args()))
        elif demisto.command() == "msgraph-list-drive-children":
            return_outputs(*list_drive_children_command(client, demisto.args()))
        elif demisto.command() == "msgraph-create-new-folder":
            return_outputs(*create_new_folder_command(client, demisto.args()))
        elif demisto.command() == "msgraph-replace-existing-file":
            return_outputs(*replace_an_existing_file_command(client, demisto.args()))
        elif demisto.command() == "msgraph-list-drives-in-site":
            return_outputs(*list_drives_in_site_command(client, demisto.args()))
        elif demisto.command() == "msgraph-upload-new-file":
            return_outputs(*upload_new_file_command(client, demisto.args()))

    # Log exceptions
    except Exception as e:
        return_error(
            f"Failed to execute {demisto.command()} command. Error: {str(e)}", e
        )


""" COMMANDS + REQUESTS FUNCTIONS """


def module_test(client, args=None):
    """
    Performs basic get request to get item samples
    """
    result = client.get_access_token()
    if result:
        return "ok"
    else:
        return "Test failed because could not get access token"


if __name__ in ("__main__", "__builtin__", "builtins"):
    main()
