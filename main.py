from typing import Any

import kernel.webhook as Webhook
import kernel.ipc as IPC
import kernel.journaling as Journaling
import kernel.registry as Registry
import kernel.io as IO
import kernel.partitionmgr as PartitionManager

from kernel.objects.embedmsg import EmbeddedMessage

from googleapiclient.discovery import build
from google.oauth2 import service_account

import time


def pullFileList():
    credentialJson = Registry.read("SOFTWARE.CordOS.Kernel.Services.GoogleDrive.Credentials", default="storage/services/GoogleDriveTracker/credentials.json", writeDefault=True)
    credentials = service_account.Credentials.from_service_account_file(credentialJson, scopes=['https://www.googleapis.com/auth/drive'])
    service = build("drive", "v3", credentials=credentials)
    while IPC.canRepeatUntilShutdown():
        try:
            results = service.files().list(fields="files(name, kind, id, name, parents)").execute()
            items = results.get('files', [])
            return {"files": items}
        except Exception as e:
            Journaling.record("ERROR", "Failed to pull file list: " + str(e))
            Journaling.record("INFO", "Retrying in 5 seconds")
            time.sleep(5)


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
            diff.append(("rename", searchedItem[1], item[1]))
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

    def dropRoot(path: str) -> str:
        pathComponents = path.split("/")
        if len(pathComponents) > 2:
            pathComponents.pop(0)
        return "/".join(pathComponents)

    while IPC.canRepeatUntilShutdown():
        try:
            time.sleep(3)
            newIndex = index(restructure(pullFileList()))
            diff = traceDifference(originalIndex, newIndex)
            outputStr = ""
            for item in diff:
                if item[0] == "add":
                    Journaling.record("INFO", f"New file added: {item[2]}")
                    outputStr += f"**Added**\n{dropRoot(item[2])}\n\n"

                elif item[0] == "remove":
                    Journaling.record("INFO", f"File removed: {item[1]}")
                    outputStr += f"**Removed**\n{dropRoot(item[1])}\n\n"

                elif item[0] == "rename":
                    Journaling.record("INFO", f"File moved: {item[1]} -> {item[2]}")
                    backupOutputStr = outputStr
                    outputStr += f"**State Changed**\n"
                    # outputStr += f"From: {dropRoot(item[1])}\n"
                    # outputStr += f"To: {dropRoot(item[2])}\n\n"
                    fileName = item[1].split("/")[-1]
                    if "Draft" in item[1]:
                        originalState = "Draft"
                    elif "Feedback Queue" in item[1]:
                        originalState = "Feedback Queue"
                    elif "Archive" in item[1]:
                        originalState = "Archive"
                    elif "Published" in item[1]:
                        originalState = "Published"
                    else:
                        originalState = "Unsorted"
                    if "Draft" in item[2]:
                        newState = "Draft"
                    elif "Feedback Queue" in item[2]:
                        newState = "Feedback Queue"
                    elif "Archive" in item[2]:
                        newState = "Archive"
                    elif "Published" in item[2]:
                        newState = "Published"
                    else:
                        newState = "Unsorted"
                    outputStr += f"「 {fileName} 」\n"
                    outputStr += f"From: {originalState}\n"
                    outputStr += f"To: {newState}\n\n"

                    if originalState == newState:
                        outputStr = backupOutputStr
                        outputStr += f"**Renamed**\n{dropRoot(item[1])} -> {dropRoot(item[2])}\n\n"

            originalIndex = newIndex
            try:
                if outputStr == "":
                    Journaling.record("INFO", "No changes detected")
                else:
                    # Webhook.send(Registry.read("SOFTWARE.CordOS.Kernel.Services.GoogleDrive.WebhookURL", default="", writeDefault=True), outputStr)
                    webhookUrl = Registry.read("SOFTWARE.CordOS.Kernel.Services.GoogleDrive.WebhookURL", default="", writeDefault=True)
                    Journaling.record("INFO", f"Sending webhook (URL: {webhookUrl})")
                    message = EmbeddedMessage(
                        message=None,
                        title="Google Drive Tracker",
                        description=outputStr,
                        color=0x00ff00,
                        footer="Google Drive Tracker"
                    )
                    try:
                        # CoroutineResolve.runAsync(message.sendAsWebhook)
                        Webhook.sendEmbed(webhookUrl, message)
                        Journaling.record("INFO", "Webhook sent")
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        Journaling.record("ERROR", "Failed to send webhook as embed message: " + str(e))
                        stackTrace = traceback.format_exc()
                        Webhook.send(webhookUrl, "Warning: Failed to send webhook as embed message. Stack trace:\n" + stackTrace)
                        Webhook.send(webhookUrl, outputStr)
            except Exception as e:
                Journaling.record("ERROR", "Failed creating webhook message: " + str(e))
                import traceback
                traceback.print_exc()
                stackTrace = traceback.format_exc()
                Webhook.send(Registry.read("SOFTWARE.CordOS.Kernel.Services.GoogleDrive.WebhookURL", default="", writeDefault=True), "Warning: Failed creating webhook message. Stack trace:\n" + stackTrace)
                Webhook.send(Registry.read("SOFTWARE.CordOS.Kernel.Services.GoogleDrive.WebhookURL", default="", writeDefault=True), outputStr)
        except Exception as e:
            Journaling.record("ERROR", "Error in main loop: " + str(e))
            import traceback
            traceback.print_exc()
            stackTrace = traceback.format_exc()
            Webhook.send(Registry.read("SOFTWARE.CordOS.Kernel.Services.GoogleDrive.WebhookURL", default="", writeDefault=True), "Warning: Error in main loop. Stack trace:\n" + stackTrace)
