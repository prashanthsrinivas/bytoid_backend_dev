# Frontend Contract — Policy Hub Card Approval Status

> Hand this to the frontend (or paste into Lovable) to show the approval status
> on each policy / procedure / standard card in the second pane (the document
> list). Every field below matches the deployed backend exactly. The endpoint is
> unchanged — only additive fields. Auth/permissions are unchanged.

## GET `/policy-hub/list`

- **Query params:** `user_id` (required)
- **Permission:** `policyhub.view`
- Returns `{ "items": [ ... ] }`. Each item now carries an **always-present**
  approval status alongside the existing `workflow_state`:

```json
{
  "items": [
    {
      "policy_id": "pol_ab12",
      "title": "Credential Lifecycle Management Policy",
      "type": "policy",                       // policy | procedure | standard
      "workflow_state": "draft",              // raw state, may be null (no workflow row yet)
      "approval_status": "draft",             // NEVER null — defaults to "draft"
      "approval_status_label": "Draft",       // human label, ready to render
      "is_published": false                   // true only when fully approved/published
    }
  ]
}
```

### Field reference

| field                   | type        | meaning                                                        |
|-------------------------|-------------|----------------------------------------------------------------|
| `approval_status`       | enum string | Current workflow stage. Never null.                            |
| `approval_status_label` | string      | Display label for `approval_status`.                           |
| `is_published`          | bool        | `true` once the doc reaches `published` (final/approved).      |
| `workflow_state`        | enum / null | Raw state; `null` if the doc has no workflow row yet.          |

### Enum → label mapping (all three doc types use the same set)

| `approval_status`    | `approval_status_label` |
|----------------------|-------------------------|
| `draft`              | `Draft`                 |
| `quality_review`     | `Quality Review`        |
| `governance_review`  | `Governance Review`     |
| `approval`           | `Approval`              |
| `published`          | `Published`             |

**UI guidance for the second-pane cards:**
- Render a status badge on every card using `approval_status_label` (prefer this
  over mapping `workflow_state` yourself — it's always present, including for
  documents that haven't entered the workflow yet, which show as `Draft`).
- Use `is_published` to style the "approved/final" badge distinctly (e.g. green)
  vs. in-progress stages.
- This applies uniformly to `policy`, `procedure`, and `standard` cards — all
  three are workflow-supported and return the same fields.
