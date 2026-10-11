# Rolebot signed Google Drive gateway

This Apps Script lets the AWS bot create and update its generated XLSX files as
your Google user without giving the bot your Google password, refresh token, or
general Drive access. The endpoint accepts only signed requests for Rolebot
event and pass workbooks in one configured folder.

## Deploy it manually

1. Open <https://script.google.com>, create a new standalone project, and name
   it `Rolebot Drive Gateway`.
2. Replace the contents of `Code.gs` with the repository's `Code.gs`.
3. Open **Project Settings**, enable **Show "appsscript.json" manifest file in
   editor**, and replace that file with the repository's `appsscript.json`.
   Alternatively, add **Drive API** under **Services** in the editor.
4. In **Project Settings > Script properties**, add:

   - `ROLEBOT_FOLDER_ID`: `1mQwgP3f0YwCWlrOSbvU4jCe0rcXsqoe7`
   - `ROLEBOT_HMAC_SECRET`: the output of `openssl rand -hex 32`

   Keep the secret out of source control and chat.
5. Choose **Deploy > New deployment > Web app**. Set **Execute as** to **Me**
   and **Who has access** to **Anyone**. Authorize the requested Drive access,
   deploy, and copy the URL ending in `/exec`.
6. Run the non-mutating health check from the repository root:

   ```bash
   python integrations/google-drive-apps-script/check_endpoint.py \
     'https://script.google.com/macros/s/DEPLOYMENT_ID/exec'
   ```

   Paste `ROLEBOT_HMAC_SECRET` into the hidden prompt.

   A successful response contains `"ok": true` and
   `"folder_name": "roleclub"`.

When updating the Apps Script later, create a new version under **Deploy >
Manage deployments > Edit**. Keep the existing deployment and `/exec` URL.

For AWS deployment, store the same signing value as the
`GOOGLE_APPS_SCRIPT_HMAC_SECRET` secret in the GitHub `production` environment.
Store the `/exec` URL as `GOOGLE_APPS_SCRIPT_URL` in both the `production` and
`production-plan` environments. Never put the signing value in a repository
variable or source file.

## Security boundary

- Every request is authenticated with HMAC-SHA-256 and expires after five
  minutes.
- The folder is fixed by a server-side script property.
- Only `events — *.xlsx` and `passes — *.xlsx` names are accepted.
- Uploads are capped at 10 MiB, duplicate names are rejected, and deletes only
  move a file to trash.
- Files are published as anyone-with-the-link readers so stored bot links work
  for players and administrators.

The web app must be publicly reachable because AWS does not sign in to Google.
The public URL alone grants no Drive operation; callers also need the HMAC
secret. Google account or Workspace policy can still disable public-link
sharing, in which case uploads return an error until that policy is changed.
