# Agent mailboxes can only send to dimension-x.net

Applied 2026-10-23 on the mail server (`docker-mailserver` v16, container `mail`, compose project
`/home/gordon/nvme/mailserver` on `docker-server`). The agents send through the Nextcloud Mail app, which logs in
to this server as the agent (Mara: `mail.dimension-x.net:587` TLS, Hannah: `:465` SSL), so the rule is enforced
here and holds whatever tools an agent has. The ISP's port-25 block is not a control; this is.

Mailboxes under the rule: `mara@`, `hannah@`, `hermes@` (`hermes-agent` software, currently unused), all `@dimension-x.net`.
Humans (`gordon@`, `admin@`, `gordon@gordonmclellan.com`) are not affected.
An agent mailbox may send only to a plain `local@dimension-x.net` address: no other domain (including Gordon's
`gordonmclellan.com` mailbox), no subdomains, no `%`/`!`/quoted routing tricks (Postfix `allow_percent_hack` is on).
A refused send comes back as `554 5.7.1 ... Recipient address rejected: Agent mailboxes may only send to addresses at dimension-x.net`.

## Files (config folder `/home/gordon/nvme/mailserver/docker-data/dms/config`, mounted at `/tmp/docker-mailserver`)

`agent-mailboxes` (used by both `check_sender_access` and `check_sasl_access`):
```
mara@dimension-x.net    agents_internal_only
hannah@dimension-x.net  agents_internal_only
hermes@dimension-x.net  agents_internal_only
```
`agent-recipient-rules.pcre`:
```
!/^[a-z0-9._+=-]+@dimension-x\.net$/  REJECT Agent mailboxes may only send to addresses at dimension-x.net
```
`postfix-main.cf`:
```
smtpd_restriction_classes = agents_internal_only
agents_internal_only = check_recipient_access pcre:/tmp/docker-mailserver/agent-recipient-rules.pcre
smtpd_sender_restrictions = check_sender_access texthash:/tmp/docker-mailserver/agent-mailboxes, $dms_smtpd_sender_restrictions
agents_sender_restrictions = check_sasl_access texthash:/tmp/docker-mailserver/agent-mailboxes, check_sender_access texthash:/tmp/docker-mailserver/agent-mailboxes, $mua_sender_restrictions
```
`postfix-master.cf`:
```
submission/inet/smtpd_sender_restrictions=$agents_sender_restrictions
submissions/inet/smtpd_sender_restrictions=$agents_sender_restrictions
```

The sender check keys on the address in MAIL FROM (also covers port 25), the SASL check keys on the login name (covers a null
sender). DMS's own `SPOOF_PROTECTION=1` (`reject_authenticated_sender_login_mismatch`) stops a login claiming another address.

## Gotchas learned applying it

- **A plain `docker restart mail` does NOT re-read `postfix-main.cf`/`postfix-master.cf`** ("Container was restarted. Skipping most setup routines").
  Recreate the container: `cd /home/gordon/nvme/mailserver && docker compose up -d --force-recreate mailserver` (all data is in bind mounts; about a minute of downtime).
- Editing a table file (`agent-mailboxes`, the `.pcre`) needs only `docker exec mail postfix reload`.
- `postfix-master.cf` lines are applied with `postconf -P`, which rejects values with whitespace, hence the list lives in `postfix-main.cf` as `agents_sender_restrictions` and master.cf just references it.
- In pcre tables matching is **case-insensitive by default; the `i` flag turns that off** (first version rejected `GORDON@Dimension-X.net`).
- Port 25 is behind postscreen (multi-line `220-` greeting, `mynetworks` is empty): unauthenticated mail to outside domains is "Relay access denied" before this rule matters, so test the rule on 587/465 with a login.

## Tested 2026-10-23 (throwaway mailbox, created and deleted; stops at RCPT TO, nothing sent)

Not in the list: could address `someone@example.org`, `gordon@gordonmclellan.com`, `x%example.org@dimension-x.net`, and send with a null sender.
In the list: all of those rejected on both 587 and 465; `gordon@dimension-x.net` accepted; claiming `gordon@` as sender rejected.
Not tested: a real send by Mara or Hannah (no agent credentials were used). To confirm end to end, ask an agent to email an outside address and expect the 554 text above.

## Adding or exempting an agent, or rolling back

- Add: append `name@dimension-x.net  agents_internal_only` to `agent-mailboxes`, then `docker exec mail postfix reload`.
- Exempt: delete its line, reload.
- Roll back entirely: delete `postfix-main.cf`, `postfix-master.cf`, `agent-mailboxes`, `agent-recipient-rules.pcre` from the config folder and recreate the container as above.
