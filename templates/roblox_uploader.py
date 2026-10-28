import os
import json
import secrets
import requests

from flask import Blueprint, request, render_template


roblox_uploader = Blueprint(
    "roblox_uploader",
    __name__,
    template_folder="templates",
)

MAX_BYTES = 20 * 1024 * 1024

AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


@roblox_uploader.route("/uploader", methods=["GET", "POST"])
def uploader():
    api_key = os.environ.get("ROBLOX_OPEN_CLOUD_API_KEY")
    group_id = os.environ.get("ROBLOX_GROUP_ID")
    password = os.environ.get("UPLOADER_PASSWORD")

    if not api_key or not group_id or not password:
        return render_template(
            "roblox_uploader.html",
            error="Uploader is not configured. Set the required Fly.io secrets.",
        ), 503

    if request.method == "GET":
        return render_template("roblox_uploader.html")

    if not secrets.compare_digest(request.form.get("password", ""), password):
        return render_template(
            "roblox_uploader.html",
            error="Incorrect uploader password.",
        ), 403

    audio = request.files.get("audio")
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()

    if not audio or not audio.filename or not name:
        return render_template(
            "roblox_uploader.html",
            error="Choose an audio file and enter a name.",
        ), 400

    ext = os.path.splitext(audio.filename)[1].lower()
    if ext not in AUDIO_TYPES:
        return render_template(
            "roblox_uploader.html",
            error="Use MP3, OGG, WAV, or FLAC.",
        ), 400

    content = audio.read(MAX_BYTES + 1)
    if not content or len(content) > MAX_BYTES:
        return render_template(
            "roblox_uploader.html",
            error="Audio must be non-empty and no larger than 20 MiB.",
        ), 400

    metadata = {
        "assetType": "Audio",
        "displayName": name[:50],
        "description": description[:1000],
        "creationContext": {
            "creator": {"groupId": int(group_id)}
        },
    }

    try:
        response = requests.post(
            "https://apis.roblox.com/assets/v1/assets",
            headers={"x-api-key": api_key},
            files={
                "request": (
                    None,
                    json.dumps(metadata),
                    "application/json",
                ),
                "fileContent": (
                    os.path.basename(audio.filename),
                    content,
                    AUDIO_TYPES[ext],
                ),
            },
            timeout=120,
        )

        try:
            result = response.json()
        except ValueError:
            result = {}

        if response.status_code not in (200, 201, 202):
            message = result.get("message") or result.get("title")
            return render_template(
                "roblox_uploader.html",
                error=(
                    f"Roblox returned HTTP {response.status_code}: "
                    f"{message or 'Upload failed. Check API permissions.'}"
                ),
            ), 502

        operation_path = result.get("path", "")
        if not isinstance(operation_path, str) or not operation_path.startswith("operations/"):
            return render_template(
                "roblox_uploader.html",
                error="Roblox accepted the request but returned no operation ID.",
                result=json.dumps(result, indent=2),
            ), 502

        return render_template(
            "roblox_uploader.html",
            result=json.dumps({
                "message": "Upload submitted to Roblox. Processing may continue.",
                "operation": operation_path,
            }, indent=2),
        )

    except requests.RequestException:
        return render_template(
            "roblox_uploader.html",
            error="Could not reach Roblox. Try again later.",
        ), 502


@roblox_uploader.route("/uploader/status", methods=["POST"])
def upload_status():
    api_key = os.environ.get("ROBLOX_OPEN_CLOUD_API_KEY")
    password = os.environ.get("UPLOADER_PASSWORD")

    if not api_key or not password:
        return {"message": "Uploader is not configured."}, 503

    if not secrets.compare_digest(request.form.get("password", ""), password):
        return {"message": "Unauthorized."}, 403

    operation = request.form.get("operation", "")
    if not operation.startswith("operations/"):
        return {"message": "Invalid operation."}, 400

    try:
        response = requests.get(
            "https://apis.roblox.com/assets/v1/" + operation,
            headers={"x-api-key": api_key},
            timeout=20,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"message": "Roblox returned an invalid response."}
        return data, response.status_code
    except requests.RequestException:
        return {"message": "Could not reach Roblox."}, 502
