# Integration and mobile deployment notes

1. Confirm the existing Python web framework is Flask. If your app is only a Discord bot or uses FastAPI, this blueprint needs a different integration.
2. Copy `roblox_uploader.py` into the same directory as the main Flask app.
3. Copy the `templates` folder into the app root, merging it with any existing templates directory.
4. Merge `flask` and `requests` from `requirements-uploader.txt` into your existing `requirements.txt`. Do not replace other dependencies.
5. In the main Flask file, after `app = Flask(...)`, add:
   ```python
   from roblox_uploader import roblox_uploader
   app.register_blueprint(roblox_uploader)
   ```
6. Configure Fly.io secrets:
   - `ROBLOX_OPEN_CLOUD_API_KEY`
   - `ROBLOX_GROUP_ID`
   - `UPLOADER_PASSWORD`
7. Commit changes in GitHub and deploy using your existing Fly.io workflow.
8. Visit `https://YOUR-APP.fly.dev/uploader`.

Keep API keys out of GitHub. The provided module expects Flask; do not deploy the integration unchanged if the existing app uses another framework.
