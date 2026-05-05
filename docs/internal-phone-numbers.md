# JJ internal phone numbers (RingCentral)

Pulled live from the RingCentral `account/~/phone-number` and
`account/~/extension` endpoints on 2026-05-06.

These are JJ's OWN lines — never customer phones. The Customer 360
service uses this list to short-circuit "this isn't a customer"
when an agent navigates to one of these numbers, and the call-history
ingestion pipeline filters them out so they don't pollute the
per-customer call counts.

| AU local | E.164 | Type | Usage | Ext # | Owner / label |
|---|---|---|---|---|---|
| `0353030263` | `+61353030263` | VoiceOnly | ContactCenterNumber | — | Customer Service IB (primary IVR) |
| `0353030264` | `+61353030264` | VoiceOnly | ContactCenterNumber | — | Customer Service OB (outbound IVR) |
| `0353363519` | `+61353363519` | VoiceFax | CompanyNumber | — | — |
| `0353941020` | `+61353941020` | VoiceOnly | ContactCenterNumber | — | — |
| `0370430758` | `+61370430758` | VoiceFax | DirectNumber | 2001 | Ballarat Manager |
| `0370434617` | `+61370434617` | VoiceFax | DirectNumber | 3002 | Warrack Office |
| `0370443314` | `+61370443314` | VoiceFax | DirectNumber | 1002 | Charlie Johnson |
| `0370443330` | `+61370443330` | VoiceFax | DirectNumber | 1003 | Grant Jonasson |
| `0370443359` | `+61370443359` | VoiceFax | DirectNumber | 1004 | Belinda Battistin |
| `0370443362` | `+61370443362` | VoiceFax | DirectNumber | 1005 | Tamara Webster |
| `0370443461` | `+61370443461` | VoiceFax | DirectNumber | 1006 | Dallas Redenbach |
| `0370443527` | `+61370443527` | VoiceOnly | ContactCenterNumber | — | — |
| `0370443574` | `+61370443574` | VoiceFax | DirectNumber | 1008 | Kate Vandeheuvel |
| `0370443577` | `+61370443577` | VoiceFax | DirectNumber | 2003 | Ballarat Stock desk |
| `0370443582` | `+61370443582` | VoiceFax | DirectNumber | 2004 | Ballarat Register 1 |
| `0370443583` | `+61370443583` | VoiceFax | DirectNumber | 2005 | Ballarat Register 3 |
| `0370443638` | `+61370443638` | VoiceFax | DirectNumber | 2006 | Mishalee Stulpinas |
| `0370443642` | `+61370443642` | VoiceFax | DirectNumber | 2007 | Dave Malpas |
| `0370443665` | `+61370443665` | VoiceFax | DirectNumber | 3003 | Warrack Counter |
| `0370443677` | `+61370443677` | VoiceFax | DirectNumber | 1009 | Fabio Caris |
| `0370450415` | `+61370450415` | VoiceFax | DirectNumber | 1011 | Shane Dunn |
| `0370646824` | `+61370646824` | FaxOnly | CompanyFaxNumber | — | — |
| `0370652670` | `+61370652670` | VoiceFax | MainCompanyNumber | — | Main company number |
| `0370688598` | `+61370688598` | VoiceFax | DirectNumber | 1012 | Lila Nelis |
| `0370762712` | `+61370762712` | VoiceFax | DirectNumber | 4010 | Donna Tucker |

## How it's seeded

Source of truth is the RingCentral API. For chainsaw-ops we copy this list
into a small SQLite table `internal_phone_numbers` (see migration
`f4a5b6c7d8e9_add_internal_phone_numbers.py`). Refresh by re-running
`scripts/sync_rc_internal_numbers.py` whenever a new staff member is added
or a number is provisioned.

## Categories

- **ContactCenterNumber** — the IVR DIDs customers see and dial. Inbound traffic via CXone. The `to_number` on every customer call.
- **MainCompanyNumber** / **CompanyNumber** — RC PBX-routed inbound lines.
- **DirectNumber** — staff member's direct line. When an agent dials a customer, the RC log records this as the `from_phone_number`. **This is the source of the "Bill Parker shows 2,845 calls" bug** — every outbound call from one of these lines was being keyed to the wrong "phone".
- **CompanyFaxNumber** — fax line. No voice traffic. Effectively dead for our purposes.
