from typing import Any

import kernel.webhook as Webhook
import kernel.ipc as IPC
import kernel.journaling as Journaling
import kernel.registry as Registry
import kernel.io as IO
import kernel.partitionmgr as PartitionManager

from googleapiclient.discovery import build
from google.oauth2 import service_account

import time


def pullFileList():
    credentialJson = Registry.read("SOFTWARE.CordOS.Kernel.Services.GoogleDrive.Credentials", default="storage/services/GoogleDriveTracker/credentials.json", writeDefault=True)
    credentials = service_account.Credentials.from_service_account_file(credentialJson, scopes=['https://www.googleapis.com/auth/drive'])
    service = build("drive", "v3", credentials=credentials)
    results = service.files().list(fields="files(name, kind, id, name, parents)").execute()
    items = results.get('files', [])
    return {"files": items}


def restructure(input_data: dict) -> dict:
    def find_node_by_id(node, node_id):
        if node['id'] == node_id:
            return node
        if 'children' in node:
            for child in node['children']:
                found = find_node_by_id(child, node_id)
                if found:
                    return found
        return None

    files = input_data['files']
    id_to_node = {file['id']: file for file in files if 'id' in file}
    child_ids = set()
    for file in files:
        if 'parents' in file:
            parent_id = file['parents'][0]
            parent_node = id_to_node[parent_id]
            if 'children' not in parent_node:
                parent_node['children'] = []
            parent_node['children'].append(file)
            del file['parents']
            child_ids.add(file['id'])
    root_files = [file for file in files if file['id'] not in child_ids]
    return {"children": root_files}


def decodePath(tree: dict, uid: str) -> str:
    def searchNode(obj: dict, fid: str, trace: list) -> tuple:
        if 'id' in obj.keys() and obj['id'] == fid:
            return True, trace
        if 'children' in obj.keys():
            for child in obj['children']:
                found, trace = searchNode(child, fid, trace)
                if found:
                    trace.append(child['name'])
                    return True, trace
        return False, trace

    success, pathTrace = searchNode(tree, uid, [])
    if success:
        return "/".join(pathTrace[::-1])
    else:
        return ""


def traceDifference(oldIndex: list, newIndex: list) -> list:
    def search(searchList: list, uid: str) -> Any | None:
        for subItem in searchList:
            if subItem[0] == uid:
                return subItem
        return None

    diff = []
    for item in newIndex:
        searchedItem = search(oldIndex, item[0])
        if searchedItem is None:
            diff.append(("add", None, item[1]))
        elif searchedItem[1] != item[1]:
            diff.append(("edit", searchedItem[1], item[1]))
    for item in oldIndex:
        searchedItem = search(newIndex, item[0])
        if searchedItem is None:
            diff.append(("remove", item[1], None))
    return diff


def index(tree: dict) -> list:
    def searchNode(obj: dict, trace: list) -> list:
        if 'id' in obj.keys():
            data = (obj['id'], decodePath(tree, obj['id']), obj['name'])
            trace.append(data)
        if 'children' in obj.keys():
            for child in obj['children']:
                searchNode(child, trace)
        return trace
    return searchNode(tree, [])


def main():
    if not PartitionManager.RootFS.isFile(Registry.read("SOFTWARE.CordOS.Kernel.Services.GoogleDrive.Credentials", default="storage/services/GoogleDriveTracker/credentials.json")):
        Journaling.record("ERROR", "Missing credentials.json")
        IO.println("Google Drive Tracker failed to start: Missing credentials.json")
        return

    Journaling.record("INFO", "Google Drive Tracker started")
    originalIndex = index(restructure(pullFileList()))
    Journaling.record("INFO", "Initial index created")
    Journaling.record("INFO", f"Initial index: {originalIndex}")
    while IPC.canRepeatUntilShutdown():
        time.sleep(3)
        newIndex = index(restructure(pullFileList()))
        diff = traceDifference(originalIndex, newIndex)
        outputStr = ""
        for item in diff:
            if item[0] == "add":
                Journaling.record("INFO", f"New file added: {item[2]}")
                outputStr += f"New file added: {item[2]}\n"
            elif item[0] == "remove":
                Journaling.record("INFO", f"File removed: {item[2]}")
                outputStr += f"File removed: {item[2]}\n"
            elif item[0] == "edit":
                Journaling.record("INFO", f"File edited: {item[1]} -> {item[2]}")
                outputStr += f"File edited: {item[1]} -> {item[2]}\n"
        originalIndex = newIndex
        try:
            if outputStr == "":
                Journaling.record("INFO", "No changes detected")
            else:
                Journaling.record("INFO", "Sending webhook")
                Webhook.send(Registry.read("SOFTWARE.CordOS.Kernel.Services.GoogleDrive.WebhookURL", default="", writeDefault=True), outputStr)
                Journaling.record("INFO", "Webhook sent")
        except Exception as e:
            Journaling.record("ERROR", "Failed to send webhook")
