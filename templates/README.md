# Roblox Group Audio Uploader (Flask)

A small uploader module intended to be integrated into an existing Flask app and deployed on Fly.io.

## Files
- `roblox_uploader.py`: Flask blueprint and Roblox Open Cloud API calls
- `templates/roblox_uploader.html`: mobile-friendly upload page
- `requirements-uploader.txt`: Python dependencies for the uploader
- `INTEGRATION.md`: integration and deployment notes

## Required Fly.io secrets
- `ROBLOX_OPEN_CLOUD_API_KEY`
- `ROBLOX_GROUP_ID`
- `UPLOADER_PASSWORD`

Do not put secrets in GitHub source code.

## Integration
This package expects your existing application to use Flask. In the main app file, after creating the Flask `app` instance, add:

```python
from roblox_uploader import roblox_uploader
app.register_blueprint(roblox_uploader)
```

Keep your existing bot code and dependencies. Merge `requirements-uploader.txt` into your current `requirements.txt` rather than replacing it.

## Deployment
Commit the files to the existing GitHub repository and let the configured Fly.io deployment run. Set the three secrets through Fly.io's secrets management. Visit `https://YOUR-APP.fly.dev/uploader`.

## Notes
- This is a starter implementation; it submits an upload and exposes an operation-status endpoint, but the page does not automatically poll status or guarantee moderation approval.
- Roblox API permissions, group access, quotas, supported formats, file duration, and current upload rules apply.
- Use HTTPS and keep the page private. For production, add rate limiting and CSRF protection.
- The upload endpoint's behavior can change; consult Roblox's current Open Cloud Assets API documentation if it rejects the request.
