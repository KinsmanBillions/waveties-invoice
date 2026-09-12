# WaveTies Logistics – Invoice System

## Deploy on Railway / Render

This folder is already flattened for deployment.

### Railway
1. Push this entire folder to a GitHub repo (files at the **root** of the repo).
2. New Project → Deploy from GitHub repo
3. Add environment variable:
   - `SECRET_KEY` = any long random string
4. Deploy. Railway will use the Procfile automatically.

### Render
1. New Web Service → connect the repo
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `gunicorn -b 0.0.0.0:$PORT app:app`
4. Add env var `SECRET_KEY`

### Default logins
- admin / admin123
- worker / worker123
- kofi / kofi123

**Change the admin password immediately after first login.**
