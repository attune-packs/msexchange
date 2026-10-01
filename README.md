# Microsoft Exchange Attune Pack

This pack translates StackStorm Exchange `stackstorm-msexchange` 1.1.3 to
Attune. It supports Exchange Server and Exchange Online environments where EWS
and basic/service-account credentials remain available.

## Setup

Install the root Python dependencies, ensure a Python 3.10+ runtime is
available, and create the action credential as a pack-owned encrypted Key:

```bash
attune key create -e --owner-type pack --owner-pack-ref msexchange \
  --local-ref credentials --name "Microsoft Exchange credentials" \
  --value '{"primary_smtp_address":"automation@example.invalid","username":"REDACTED","password":"REDACTED","timezone":"UTC","verify_ssl":true}'
```

Optional credential fields are `server` (disables autodiscovery) and
`verify_ssl`. Never put real credentials in pack config or rule YAML.

The managed item sensor cannot decrypt action Keys. Mount the same JSON shape
as a read-only file below `/run/secrets` on the sensor worker, then create a
rule for `msexchange.exchange_new_item` whose trigger parameters include
`credential_file`, `folder`, and polling controls. Events are targeted to the
subscribing rule. By default, an item is marked read only after Attune accepts
its event, preserving the source pack's destructive unread-item semantics.

## Actions

- `msexchange.get_calendar_items`
- `msexchange.get_folder`
- `msexchange.list_folders`
- `msexchange.save_attachments`
- `msexchange.search_items`
- `msexchange.send_email`
- `msexchange.do_attachment_directory_maintenance`

Action parameters are one flat stdin JSON document. Exchange actions accept a
`credential_key`, defaulting to `pack.msexchange.credentials`. Saved attachments go
to `ATTUNE_ARTIFACTS_DIR`; filenames are basename-normalized and made unique.
The source wrote into its pack directory, which is incompatible with Attune's
read-only pack assumption.

The disabled `msexchange.attachment_directory_maintenance` rule uses
`core.crontimer` instead of a custom daily sensor. Before enabling it, change
the example directory to an existing mutable path mounted on action workers.

## Fidelity And Operational Differences

| Source | Attune target | Fidelity | Important differences | Follow-up |
|---|---|---|---|---|
| Seven Python actions | Seven Attune actions and shared `lib/exchange.py` | adapted | Modern `exchangelib`; flat stdin JSON; normalized JSON output | Test against the deployed EWS version |
| `item_sensor` and `exchange_new_item` | Managed polling sensor and trigger | adapted | Per-rule config, bounded pages, backoff, and targeted events; still marks emitted items read | Mount a credential file and decide whether destructive `mark_read` is acceptable |
| Maintenance sensor and trigger | `core.crontimer` | adapted | Uses core scheduling instead of a custom poller | None |
| Disabled maintenance rule | Disabled Attune rule | adapted | Directory values are explicit rule action parameters | Set a worker-visible mutable directory before enabling |
| Attachment directory config | Execution artifacts plus explicit maintenance path | partial | Files are execution-scoped; no shared pack-directory attachment store | Use external durable storage if cross-execution file retention is required |
| StackStorm EWS discovery Key cache | Direct `exchangelib` discovery/configuration | partial | No persisted EWS endpoint/auth cache; each process may autodiscover | Set `server` to avoid repeated autodiscovery |
| StackStorm retry/timeout/cancellation | Worker and library defaults | partial | Source did not declare precise policies; no claim of equivalence | Set deployment-specific action timeouts and network policy |

## Assumptions

- Source is StackStorm Exchange `stackstorm-msexchange` version 1.1.3,
  licensed Apache-2.0.
- Tests do not make external calls and use synthetic credentials and objects.
- EWS is reachable from action and sensor workers. Microsoft Graph migration is
  outside this source-equivalent conversion.
- Poll ordering follows Exchange's unread query order. The source provided no
durable ordering or exactly-once guarantee; delivery remains at-least-once
around process failure between event acceptance and marking an item read.

## Upstream And License

This is a modified adaptation of the original
[StackStorm Exchange Microsoft Exchange pack](https://github.com/StackStorm-Exchange/stackstorm-msexchange).
The upstream Apache License 2.0 is included in [LICENSE](LICENSE), with
attribution details in [NOTICE](NOTICE).
