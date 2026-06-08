"""OSINT collector Lambda (separate deployable).

This subpackage is bundled and deployed to AWS Lambda by ``deploy.py``. It is a
*stateless collector*: it receives ``{assessment_id, vendor_name, vendor_domain,
callback_url, scan_id}``, runs the free/open-source OSINT collectors (all via
``vra.osint.safe_fetch``), normalizes the findings with ``vra.osint.normalize``,
HMAC-signs the snapshot with ``vra.osint.signing``, and POSTs it back to the
app's callback. It holds NO KMS keys and never touches the database — the app
owns all encryption and persistence.

Named ``collector_lambda`` (not ``lambda``) because ``lambda`` is a Python
reserved word and could not be imported as a package.
"""
