# Camera Compliance Agent

An HR tool for camera/video compliance incidents. A manager emails a report to
an HR mailbox; the system identifies the people involved, opens a Google Chat
space with the employee, the manager and HR, asks the employee for their
justification, reminds them if they go quiet, and writes an audit row when a
human closes the incident.

It does **not** monitor meetings, access video, take screenshots, judge whether
a justification is acceptable, or decide an outcome. A manager starts every
incident; a manager or HR ends it.

## Two services

| | Agent (`agent/`) | Orchestrator (`orchestrator/`) |
|---|---|---|
| What it is | An ADK agent wrapped in a Runner | A FastAPI app |
| What it does | Reads a manager's email, resolves people through the directory, classifies chat messages | Everything with a side effect |
| State | None. Every call is independent | Firestore |
| Called by | The orchestrator, over HTTP | Pub/Sub push and Cloud Scheduler |

The split is deliberate. The model reasons; it never creates a space, posts a
message, starts a timer, closes an incident, or writes the audit log. That
keeps the audit trail deterministic and makes it impossible for the model to
claim an action it did not perform.

## Flow

```
manager email
   -> Gmail watch -> Pub/Sub (incoming-mail) -> POST /mail
   -> agent INTAKE: extract + resolve people against the employee sheet
        status OK      -> create Chat space, add employee/manager/HR,
                          post the issue and the ask, subscribe to the space
        status CLARIFY -> email the manager back for the missing details,
                          open nothing, contact no one
   -> employee replies in the space
   -> Workspace Events -> Pub/Sub (chat-events) -> POST /chat
   -> agent MESSAGE: classify
        EMPLOYEE_JUSTIFICATION -> record, stop the clock
        FOLLOWUP_QUESTION      -> restart the clock
        CLOSURE_COMMAND        -> close, unsubscribe, write the audit row
   -> Cloud Scheduler -> POST /sweep every 30 min
        overdue incidents get a reminder; Gmail watch and Chat subscriptions
        get renewed; failed audit writes are retried
```

## Incident states

`AWAITING_JUSTIFICATION` → `JUSTIFICATION_RECEIVED` → (`AWAITING_FOLLOWUP` ⇄
`JUSTIFICATION_RECEIVED`) → `RESOLVED`

Only the `AWAITING_*` states carry a `due_at` and are eligible for reminders.
Reminders stop after `MAX_REMINDERS`; the incident stays open for a human.

## The two sheets

**Employee master** — shared with the service account as **Viewer**. Needs a
header row. Column names are matched case- and punctuation-insensitively:

| Recognised as | Accepted headers |
|---|---|
| `employee_name` | Employee Name, Employee, Name, Full Name |
| `employee_email` | Employee Email, Email, Work Email, Email Address |
| `manager_name` | Manager Name, Manager, Reporting Manager |
| `manager_email` | Manager Email |
| `hr_name` | HR Name, HR, HR Contact, HR Representative |
| `hr_email` | HR Email, HR Contact Email |
| `department` | Department, Team, Dept, Function |

**Audit log** — shared with the service account as **Editor**. Row 1 should be:

```
Incident ID | Employee | Manager | Meeting/type | Meeting date | Reported on |
Chat space link | Justification text | Reminders sent | Reviewed by |
Outcome | Resolved on | Closed by | Screenshot attached
```

Screenshots themselves are never stored; only whether one was attached.

## Authentication

This codebase uses **Application Default Credentials only**. There is no API
key anywhere, and no service-account JSON key is ever downloaded, mounted, or
read. Both are worth stating explicitly, because the usual tutorials for two of
the things this system does would tell you to use them:

| Where a key is normally used | What this does instead |
|---|---|
| Gemini via an API key | Vertex AI with `GOOGLE_GENAI_USE_VERTEXAI=TRUE`, authenticated by ADC |
| Domain-wide delegation via a downloaded `key.json` | `orchestrator/gcp_auth.py` has the IAM Credentials API sign the delegation JWT, so no key material exists |

On Cloud Run, ADC resolves to the attached service account automatically —
nothing to configure. The keyless delegation is what lets this run under an org
policy that blocks API keys and service-account key creation
(`constraints/iam.disableServiceAccountKeyCreation`), which normally makes the
standard domain-wide delegation flow impossible.

The one thing it needs in exchange is that the service account may sign its own
JWTs, which is the `roles/iam.serviceAccountTokenCreator` binding in setup
step 2. Without it you get `token exchange failed` in the logs.

## Setup

### 1. APIs

```bash
gcloud config set project YOUR_PROJECT_ID
export PROJECT_ID=$(gcloud config get-value project)
export REGION=us-central1

gcloud services enable \
  gmail.googleapis.com chat.googleapis.com \
  workspaceevents.googleapis.com pubsub.googleapis.com \
  run.googleapis.com cloudscheduler.googleapis.com \
  firestore.googleapis.com aiplatform.googleapis.com \
  iamcredentials.googleapis.com sheets.googleapis.com \
  cloudbuild.googleapis.com
```

### 2. Service account

```bash
gcloud iam service-accounts create camera-compliance-agent \
  --display-name="Camera Compliance Agent"
export SA=camera-compliance-agent@$PROJECT_ID.iam.gserviceaccount.com

for ROLE in roles/aiplatform.user roles/datastore.user roles/pubsub.editor \
            roles/run.invoker roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SA" --role="$ROLE"
done

# Required for keyless domain-wide delegation: the account signs its own JWTs.
gcloud iam service-accounts add-iam-policy-binding $SA \
  --member="serviceAccount:$SA" \
  --role="roles/iam.serviceAccountTokenCreator"
```

### 3. What the Workspace admin has to do — start this early

Two separate asks, and they are not the same mechanism.

**a) Domain-wide delegation, for Gmail only.** Reading a mailbox always means
acting as its owner, so there is no app-authentication path for Gmail. Get the
service account's numeric client ID:

```bash
gcloud iam service-accounts describe $SA --format='value(uniqueId)'
```

A Workspace super-admin goes to **admin.google.com → Security → Access and data
control → API controls → Domain-wide delegation → Add new**, pastes that client
ID, and authorises two scopes:

```
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
```

Deliberately narrower than `gmail.modify`: the agent reads reports and sends
clarification replies, and cannot modify, label or delete anything.

**b) Allow Chat apps to create spaces and add members.** Chat runs as the app
itself, not as a person, which is what gives each agent its own name and avatar.
That capability is gated by a Workspace setting. Without it, `spaces.create`
returns 403.

Neither blocks the agent service — only the orchestrator's Gmail and Chat calls.

### 3b. Configure the Chat app

In the Cloud console: **APIs & Services → Enabled APIs → Google Chat API →
Configuration**.

| Field | Value |
|---|---|
| App name | `Camera Compliance Agent` — this is the name employees see in the space |
| Avatar URL | any hosted image; it becomes the agent's face in Chat |
| Description | `Handles reported camera-compliance incidents` |
| Functionality | enable **Receive 1:1 messages** and **Join spaces and group conversations** |
| Connection settings | **App URL** is not required — this app is driven by the API and Workspace Events, not by slash commands |
| Visibility | make it available to your domain, or to specific test users first |
| App status | **Live** |

The app's identity is the service account already attached to Cloud Run, so
there is nothing further to connect.

### 4. Firestore

```bash
gcloud firestore databases create --location=$REGION
gcloud firestore indexes composite create --collection-group=incidents \
  --field-config=field-path=state,order=ascending \
  --field-config=field-path=due_at,order=ascending
gcloud firestore indexes composite create --collection-group=incidents \
  --field-config=field-path=employee_email,order=ascending \
  --field-config=field-path=state,order=ascending
gcloud firestore indexes composite create --collection-group=incidents \
  --field-config=field-path=state,order=ascending \
  --field-config=field-path=audit_written,order=ascending
```

(The same three indexes are in `firestore.indexes.json` if you prefer
`firebase deploy --only firestore:indexes`.)

### 5. Pub/Sub

```bash
gcloud pubsub topics create incoming-mail
gcloud pubsub topics create chat-events

# Gmail publishes as a fixed Google-owned service account.
gcloud pubsub topics add-iam-policy-binding incoming-mail \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

# Workspace Events publishes as the Chat service account.
gcloud pubsub topics add-iam-policy-binding chat-events \
  --member="serviceAccount:chat-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

### 6. Deploy the agent first

The orchestrator needs its URL.

```bash
gcloud run deploy camera-agent \
  --source ./agent --region $REGION \
  --service-account $SA --no-allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,\
GOOGLE_GENAI_USE_VERTEXAI=TRUE,\
GOOGLE_CLOUD_LOCATION=global,\
MODEL_ID=gemini-2.5-flash,\
EMPLOYEE_SHEET_ID=<employee sheet id>,\
EMPLOYEE_SHEET_RANGE=Employees!A:Z"

export AGENT_URL=$(gcloud run services describe camera-agent \
  --region $REGION --format='value(status.url)')
```

### 7. Deploy the orchestrator

```bash
gcloud run deploy camera-orchestrator \
  --source ./orchestrator --region $REGION \
  --service-account $SA --no-allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,\
COMPLIANCE_MAILBOX=camera-compliance@yourcompany.com,\
SERVICE_ACCOUNT_EMAIL=$SA,\
AGENT_URL=$AGENT_URL,\
AUDIT_SHEET_ID=<audit sheet id>,\
GMAIL_TOPIC=projects/$PROJECT_ID/topics/incoming-mail,\
CHAT_EVENTS_TOPIC=projects/$PROJECT_ID/topics/chat-events,\
RESPONSE_PERIOD_HOURS=12"

export ORCH_URL=$(gcloud run services describe camera-orchestrator \
  --region $REGION --format='value(status.url)')
```

### 8. Wire the push subscriptions and the schedule

```bash
gcloud pubsub subscriptions create mail-to-orch \
  --topic incoming-mail --push-endpoint="$ORCH_URL/mail" \
  --push-auth-service-account=$SA --ack-deadline=120

gcloud pubsub subscriptions create chat-to-orch \
  --topic chat-events --push-endpoint="$ORCH_URL/chat" \
  --push-auth-service-account=$SA --ack-deadline=120

gcloud scheduler jobs create http reminder-sweep \
  --schedule="*/30 * * * *" --uri="$ORCH_URL/sweep" \
  --http-method=POST --oidc-service-account-email=$SA \
  --location=$REGION
```

### 9. Arm the Gmail watch

No APIs Explorer needed — the orchestrator does it:

```bash
curl -X POST "$ORCH_URL/admin/start-watch" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)"
```

The watch expires after 7 days; `/sweep` re-arms it automatically once it is
within `WATCH_RENEW_WITHIN_DAYS` of expiry, so this is a one-time call.

## Testing in stages

Gmail needs domain-wide delegation and Chat needs a Workspace setting, both of
which a super-admin has to grant. Everything else can be tested before that
lands.

### Stage 1 — logic only (no cloud, no credentials)

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

### Stage 2 — the agent against real Gemini and a real sheet (no admin needed)

The agent service touches only Vertex AI and the employee sheet, so it can be
exercised as soon as the sheet is shared with the service account:

```bash
gcloud run deploy camera-agent --source ./agent --region $REGION \
  --service-account $SA --no-allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_GENAI_USE_VERTEXAI=TRUE,\
GOOGLE_CLOUD_LOCATION=global,MODEL_ID=gemini-2.5-flash,EMPLOYEE_SHEET_ID=<id>"

AGENT_URL=$(gcloud run services describe camera-agent --region $REGION \
  --format='value(status.url)')

curl -s -X POST "$AGENT_URL/invoke" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H 'Content-Type: application/json' -d '{
    "payload": {
      "mode": "INTAKE",
      "raw_text": "Priya did not have her camera on during this morning DSM",
      "reported_by": "manager@yourcompany.com",
      "received_at": "2026-09-23T09:30:00Z",
      "screenshot_attached": false
    }
  }' | python3 -m json.tool
```

This is the milestone worth reaching first. It proves the hardest part: that a
free-form sentence resolves to the right person, that the manager and HR come
back from the sheet, and that an ambiguous name returns `CLARIFY` rather than a
guess. Try all three — a clear name, a name that appears twice in the sheet,
and a name that is not in the sheet at all.

### Stage 3 — full flow (needs the admin approval from setup step 3)

Shorten the clock first so a reminder arrives in minutes rather than hours:

```bash
gcloud run services update camera-orchestrator --region $REGION \
  --update-env-vars RESPONSE_PERIOD_MINUTES=2
```

Then walk one incident through with three test accounts, triggering the sweep
by hand instead of waiting for the scheduler:

1. Mail the compliance mailbox as the manager
2. Confirm a Chat space appears with all three people in it
3. Say nothing as the employee; `curl -X POST "$ORCH_URL/sweep"` and watch the
   reminder land
4. Reply as the employee; the logs show `EMPLOYEE_JUSTIFICATION`
5. Post `resolved` as the manager; check the audit sheet

Remove `RESPONSE_PERIOD_MINUTES` when you are done to fall back to the
12-hour default.

## Testing it end to end

Send a mail to the compliance mailbox from a manager's account:

> Subject: Camera off in standup
>
> Priya had her camera off for the whole of this morning's daily standup.

Then check, in order:

1. `gcloud run services logs read camera-orchestrator --region $REGION` shows
   `incident=CAM-… opened space=spaces/…`
2. A Chat space appears with the employee, manager and HR in it
3. Reply as the employee; the logs show
   `classification=EMPLOYEE_JUSTIFICATION`
4. Reply as the manager with "resolved"; the space gets a closure message and
   a row lands in the audit sheet

## Local development

`google-adk` does not yet support Python 3.14 — use 3.12:

```bash
python3.12 -m venv .venv && source .venv/bin/activate

pip install -r agent/requirements.txt
cp .env.example .env   # fill it in
set -a && source .env && set +a
```

Locally there is no metadata server, so ADC has to come from your own login.
Either run your organization's ADC setup script, or:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project $PROJECT_ID
```

Two things behave differently under a user identity rather than the service
account:

* **Delegated calls** (Gmail only) mint their token by signing a JWT as
  `SERVICE_ACCOUNT_EMAIL`. Your user needs
  `roles/iam.serviceAccountTokenCreator` on that service account to do it:

  ```bash
  gcloud iam service-accounts add-iam-policy-binding $SA \
    --member="user:$(gcloud config get-value account)" \
    --role="roles/iam.serviceAccountTokenCreator"
  ```

* **Calls to the agent service** cannot fetch an OIDC token without a metadata
  server. `agent_client.py` logs a warning and calls unauthenticated, which is
  fine when you are running the agent locally too.

The agent on its own needs neither of those — it only reads the employee sheet,
so user ADC with access to that sheet is enough:

```bash
# Browser dev UI for the agent on its own:
cd agent && adk web

# Or the HTTP surface both services speak:
cd agent && uvicorn server:app --port 8081
curl localhost:8081/invoke -H 'Content-Type: application/json' -d '{
  "payload": {
    "mode": "INTAKE",
    "raw_text": "Priya had her camera off in this mornings standup",
    "reported_by": "manager@yourcompany.com",
    "received_at": "2026-09-23T09:30:00Z",
    "screenshot_attached": false
  }
}'
```

## Tests

The tests stub every Google client, so they run anywhere with no project, no
credentials and no network:

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

They cover the places where a bug would reach a real person:

| File | What it pins |
|---|---|
| `test_schemas.py` | the model's output contract, including the downgrade of a half-resolved `OK` to `CLARIFY` |
| `test_directory.py` | matching — in particular that two employees sharing a first name come back flagged ambiguous rather than guessed at |
| `test_intake.py` | what a manager's email actually causes: one space, three members, a watch, an acknowledgement that promises no outcome, and nothing at all on `CLARIFY` |
| `test_chat_handler.py` | classification to action, and the role gate — an employee or a stranger cannot close an incident even when the model classifies their message as a closure |
| `test_roles.py` | who counts as employee, manager and HR |
| `test_audit.py` | the audit row stays aligned with its header, and screenshots are recorded as a flag rather than stored |
| `test_routes.py` | every Pub/Sub and Scheduler endpoint is served |

## Design notes

**Why the agent has no enforced output schema.** ADK does not allow an
`LlmAgent` to have both `tools` and an `output_schema` — setting a schema
disables tool use. Since the agent needs `lookup_directory`, the contract is
enforced downstream instead: `agent/camera_agent/schemas.py` parses and
validates every response, retries once with a repair prompt, and degrades an
unparseable INTAKE to `CLARIFY` rather than failing the incident.

**Why validation can downgrade `OK` to `CLARIFY`.** A half-resolved `OK` — a
missing manager email, a meeting with no date — would create a space with the
wrong people in it. `IntakeResult` checks the fields the model claimed to
resolve and downgrades if any are empty, so a hallucination becomes a question
to the manager rather than an incident against the wrong person.

**Why sender role is computed, not taken from the model.** Only a manager or HR
may close an incident, which makes role an authorization decision. The
orchestrator derives it from the sender's email against the incident record and
uses its own value to gate every action; the model's echo is advisory only.

**Why no service-account key file.** Domain-wide delegation normally wants a
downloaded JSON key. `orchestrator/gcp_auth.py` instead has the IAM Credentials
API sign the delegation JWT, so no key material exists to leak.

**Why Chat runs as an app and Gmail does not.** Chat supports app
authentication: the service account is the Chat app, so it posts under its own
name and avatar and needs no impersonation. That matters for a roadmap of many
HR agents, because each one gets a distinct identity in Chat instead of all of
them appearing as the shared automation mailbox. Gmail has no equivalent -
reading a mailbox means acting as its owner - so it alone needs domain-wide
delegation, and the approved scope list is one line rather than six.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Email arrives, nothing happens | Is the watch armed? `POST /admin/start-watch`. Then check the `mail-to-orch` subscription for unacked messages |
| 401/403 on Gmail calls | Domain-wide delegation (step 3a) — both Gmail scopes must be authorised for this client ID |
| 403 on `spaces.create` | The Workspace setting in step 3b is off, or the Chat app in step 3b is not Live |
| `token exchange failed` in logs | The service account is missing `roles/iam.serviceAccountTokenCreator` on itself |
| Space is created but replies never arrive | The Workspace Events subscription failed; check the logs at incident creation and confirm the `chat-events` topic grants publish to `chat-api-push@system.gserviceaccount.com` |
| Every report comes back as CLARIFY | The employee sheet is not shared with the service account, or its headers do not match the table above |
| Firestore `FAILED_PRECONDITION … index` | Create the three composite indexes in step 4 |
| Reminders never fire | Cloud Scheduler job is in a different region, or `/sweep` is returning non-200 — read its logs |
