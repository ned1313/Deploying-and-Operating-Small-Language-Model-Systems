# Demo 6.2: Audit a resolution without unrestricted logging

Goal: follow an authenticated request from intake through tool access, approval, and the recorded
action; reconstruct it from the audit trail; show that operational logs carry no content, that captured
content is encrypted and readable only by the auditor, that the trail is tamper-evident, and that a
deletion request removes content while keeping the record of what happened. Then show what LangSmith
receives and the difference between stopping and resetting.

Prerequisites: Demo 6.1 finished (Authelia mode, `secrets/audit/audit_content_key` created by
`setup-app-auth.sh`). Commands run from `app/` in WSL.

---

## Clip 4: capturing and storing audit data

### 1. One resolution, end to end

```bash
podman-compose run --rm cli --actor admin-01 reset CASE-10431
podman-compose run --rm cli submit A01                       # rep-kop-01
podman-compose run --rm cli approve CASE-10431               # sup-kop-01
```

### 2. Operational logs: identifiers, not content

```bash
podman-compose logs --tail 30 complaint-api | grep CASE-10431
podman-compose logs --tail 30 app-gateway
podman-compose logs --tail 30 complaint-api | grep -ci carnitas            # 0
podman-compose logs complaint-api app-gateway | grep -c "$(cat ../secrets/users/password)"   # 0
```

The API logs carry the request id, case, actor, status, and timings; app-gateway logs the signed-in user
but not the password or session cookie; on the GPU host, `gateway-secure` logs the service identity and
certificate serial. None of them hold the complaint, tool results, or the proposal.

### 3. The audit trail

```bash
podman-compose run --rm cli audit CASE-10431
podman-compose run --rm cli audit CASE-10431 --content | sed -n '1,20p'   # sed reads to the end: no broken pipe
```

Events, in order: `intake` (complaint), four `model_call` + `tool_access` pairs (each tool's arguments
and result), the proposal `model_call`, `proposal_validated` (with the proposal's SHA-256),
`workflow_run`, `review_decision` by `sup-kop-01` (same SHA-256: the reviewer approved exactly this
version), `action_recorded` (idempotency key and action id), and a second `workflow_run`. Every event
carries the request id, case, thread, actor, role, and deployment versions; only the final response of
each model call is captured (full prompts are in LangSmith).

In the browser, sign in as `aud-01` and open `https://app.taco.internal:8443/ui/audit`: the newest events
across all cases with the hash-chain result at the top, and a case box that opens
`/ui/audit/CASE-10431`, the same timeline with each event's metadata and decrypted content under
**details**. The case page links to it too ("Audit trail for this case").

### 4. Who can read it

```bash
podman-compose run --rm cli --actor sup-area-01 audit CASE-10431 | sed -n '1,3p'   # metadata only
podman-compose run --rm cli --actor sup-kop-01 audit CASE-10431              # 403
podman-compose run --rm cli --actor rep-kop-01 audit CASE-10431              # 403
podman run --rm -v taco-app-audit:/a docker.io/library/nginx:1.30.6 sh -c 'grep -c -i carnitas /a/*'  # 0: encrypted
```

Each read is itself an `audit_read` event (`cli audit CASE-10431` again shows them). Denied requests from
Demo 6.1 are `access_denied` events with the route, status, and reason. Credentials never reach the
audit store: Authelia checks the password, and the API only sees the user name. In the browser, sign out
and back in as `sup-area-01`: `/ui/audit/CASE-10431` says "Metadata only for your role" and has no
content or delete button; as `sup-kop-01` the page returns 403.

### 5. Tamper evidence

```bash
podman-compose run --rm cli audit-verify          # {"ok": true, "events": N, "head": "..."}
```

Each event stores the hash of the previous one; editing, inserting, or deleting a row breaks the chain
from that row on (the app test suite demonstrates an edited `actor_id`). This detects tampering; it does
not prevent it, and an attacker with the database could rebuild the chain, which is why production audit
trails go to an append-only store owned by a different team.

### 6. A deletion request

```bash
podman-compose run --rm cli audit-delete CASE-10431
podman-compose run --rm cli audit CASE-10431 --content | grep -c '"deleted": true'
podman-compose run --rm cli show CASE-10431          # complaint: None, outcome content_deleted
podman-compose run --rm cli audit-verify             # still ok
```

Or in the browser as `aud-01`: **Delete content** at the bottom of `/ui/audit/CASE-10431`; the content
under each event's details then reads `{"deleted": true}` and the chain on `/ui/audit` still verifies.

Deletion removes the case's audit content, its quality sample, and its workflow checkpoint; it keeps the
case record, the recorded action in the case API, and every audit event (with a `case_content_deleted`
event). Retention periods and legal holds are policy decisions covered on the slides; they decide when
this command runs, not what it does.

### 7. Trace export follows the same content policy

With `LANGSMITH_TRACING=true` and `TRACE_CONTENT_POLICY=redacted` in `app/.env`
(`podman-compose up -d --force-recreate complaint-api`), submit A02 and open its trace: the graph, tool
names, case and order ids, timings, and token counts are there; complaint text, tool results, and the
customer message show `[redacted]`. `none` hides all inputs and outputs; `full` is the module 4 behaviour.

### 8. Stopping is not resetting

```bash
podman-compose stop complaint-api case-api && podman-compose up -d case-api complaint-api
podman-compose run --rm cli --actor sup-kop-01 show CASE-10431          # still there: volumes survive a stop
podman-compose stop complaint-api case-api
podman-compose run --rm reset                        # lists what it would delete; deletes nothing
podman-compose run --rm reset --yes                  # workflow state, quality store, case records, seed data
podman-compose up -d case-api complaint-api
podman-compose run --rm cli audit-verify             # the audit trail is untouched
```

`reset` refuses to run while the APIs are up and does not mount the audit volume, so a demo reset cannot
erase the audit trail. Never use `podman-compose down --volumes` as a shutdown command.

Talking points: audit records support a compliance programme (who did what, with what data, under which
versions) but do not establish compliance by themselves; content capture needs minimization, encryption,
restricted readers, retention, and deletion decided before it is switched on.
