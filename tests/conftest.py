"""Shared pytest fixtures for Policy Hub V2 tests."""

import pytest

# Re-export the db_stubs fixture so tests anywhere in this tree can request it
# via `db_stubs` without explicit imports.
from tests._db_stubs import db_stubs  # noqa: F401


# ── Fixture HTML documents ────────────────────────────────────────────────────

POLICY_V2_HTML = """
<div class="policy-document">
  <div data-section-id="policy.header">
    <h2>Document Header</h2>
    <table>
      <tr><td>Policy Name</td><td>Access Control Policy</td></tr>
      <tr><td>Document ID</td><td>POL-001</td></tr>
      <tr><td>Version</td><td>1.0</td></tr>
      <tr><td>Effective Date</td><td>2026-06-01</td></tr>
      <tr><td>Classification</td><td>Internal</td></tr>
    </table>
  </div>
  <div data-section-id="policy.purpose">
    <h2>Purpose</h2>
    <p>This policy defines access control requirements.</p>
  </div>
  <div data-section-id="policy.scope">
    <h2>Scope</h2>
    <p>Applies to all employees and contractors.</p>
  </div>
  <div data-section-id="policy.statements">
    <h2>Policy Statements</h2>
    <ul>
      <li data-statement-id="stmt-001">All users must authenticate with MFA.</li>
      <li data-statement-id="stmt-002">Privileged access must be reviewed quarterly.</li>
      <li data-statement-id="stmt-003">Shared accounts are prohibited.</li>
    </ul>
  </div>
  <div data-section-id="policy.roles">
    <h2>Roles and Responsibilities</h2>
    <p>The CISO owns this policy.</p>
  </div>
  <div data-section-id="policy.compliance">
    <h2>Compliance and Enforcement</h2>
    <p>Violations result in disciplinary action.</p>
  </div>
  <div data-section-id="policy.exceptions">
    <h2>Exceptions</h2>
    <p>Exceptions must be approved by the CISO.</p>
  </div>
  <div data-section-id="policy.related_documents">
    <h2>Related Documents</h2>
    <p>ISO 27001 A.9, NIST SP 800-53 AC-2.</p>
  </div>
  <div data-section-id="policy.revision_history">
    <h2>Review and Revision History</h2>
    <table>
      <tr><th>Version</th><th>Date</th><th>Author</th><th>Summary</th></tr>
      <tr><td>1.0</td><td>2026-06-01</td><td>J. Smith</td><td>Initial release</td></tr>
    </table>
  </div>
</div>
"""

PROCEDURE_V2_HTML = """
<div class="policy-document">
  <div data-section-id="procedure.header">
    <h2>Document Header</h2>
    <table>
      <tr><td>Procedure Name</td><td>User Offboarding Procedure</td></tr>
      <tr><td>Document ID</td><td>PRO-001</td></tr>
      <tr><td>Version</td><td>1.0</td></tr>
      <tr><td>Effective Date</td><td>2026-06-01</td></tr>
      <tr><td>Classification</td><td>Internal</td></tr>
    </table>
  </div>
  <div data-section-id="procedure.purpose">
    <h2>Purpose</h2>
    <p>Defines the steps for revoking access when an employee leaves.</p>
  </div>
  <div data-section-id="procedure.scope">
    <h2>Scope</h2>
    <p>Applies to all employee terminations and resignations.</p>
  </div>
  <div data-section-id="procedure.prerequisites">
    <h2>Prerequisites</h2>
    <p>HR must initiate the offboarding request in the ticketing system.</p>
  </div>
  <div data-section-id="procedure.roles">
    <h2>Roles</h2>
    <p>IT Operations: responsible for access revocation.</p>
  </div>
  <div data-section-id="procedure.steps">
    <h2>Procedure Steps</h2>
    <ol>
      <li data-statement-id="step-001">Receive offboarding ticket from HR.</li>
      <li data-statement-id="step-002">Disable the user account in Active Directory.</li>
      <li data-statement-id="step-003">Revoke all SaaS application access.</li>
    </ol>
  </div>
  <div data-section-id="procedure.io">
    <h2>Inputs and Outputs</h2>
    <p>Input: HR offboarding ticket. Output: Access revocation confirmation email.</p>
  </div>
  <div data-section-id="procedure.exceptions">
    <h2>Exception Handling</h2>
    <p>Escalate to CISO if access cannot be revoked within 24 hours.</p>
  </div>
  <div data-section-id="procedure.evidence">
    <h2>Evidence / Records</h2>
    <p>Ticket closure record retained for 3 years.</p>
  </div>
  <div data-section-id="procedure.related_documents">
    <h2>Related Documents</h2>
    <p>Access Control Policy POL-001.</p>
  </div>
  <div data-section-id="procedure.revision_history">
    <h2>Revision History</h2>
    <table>
      <tr><th>Version</th><th>Date</th><th>Author</th><th>Summary</th></tr>
      <tr><td>1.0</td><td>2026-06-01</td><td>J. Smith</td><td>Initial release</td></tr>
    </table>
  </div>
</div>
"""

LEGACY_POLICY_HTML = """
<div>
  <h1>Access Control Policy</h1>
  <h2>Purpose</h2>
  <p>This policy defines access control requirements.</p>
  <h2>Scope</h2>
  <p>Applies to all employees and contractors.</p>
  <h2>Policy Statements</h2>
  <ul>
    <li>All users must authenticate with MFA.</li>
    <li>Privileged access must be reviewed quarterly.</li>
  </ul>
  <h2>Roles and Responsibilities</h2>
  <p>The CISO owns this policy.</p>
  <h2>Compliance and Enforcement</h2>
  <p>Violations result in disciplinary action.</p>
  <h2>Exceptions</h2>
  <p>Exceptions must be approved by the CISO.</p>
  <h2>Related Documents</h2>
  <p>ISO 27001 A.9.</p>
  <h2>Review and Revision History</h2>
  <table><tr><td>1.0</td><td>Initial</td></tr></table>
</div>
"""


@pytest.fixture
def policy_v2_html():
    return POLICY_V2_HTML


@pytest.fixture
def procedure_v2_html():
    return PROCEDURE_V2_HTML


@pytest.fixture
def legacy_policy_html():
    return LEGACY_POLICY_HTML
