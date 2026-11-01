"""Preparing approved project files for AI understanding (no provider connected).

Pipeline, with two explicit approval gates:

1. :func:`assess_run` - conservative, metadata-only eligibility. Reads no files.
2. :func:`propose_selection` / :func:`approve_files` - **Gate 1**: the user
   approves which files of an accepted group may be read locally.
3. :func:`build_evidence_pack` - reads only approved files through
   :class:`ApprovedFileReader`, extracts redacted, source-referenced evidence.
   Nothing is executed.
4. :func:`build_manifest` / :func:`approve_manifest` - **Gate 2**: the exact
   payload, provider and model, approved by SHA-256.
5. :func:`submit` - the only path to a provider; refuses anything unapproved.
   Only :class:`DryRunProvider` exists, and it sends nothing.

See docs/design/project-understanding-and-naming.md.
"""

from .disclosure import (
    DisclosureNotApproved,
    DryRunProvider,
    ProviderNotConfigured,
    approve_manifest,
    build_manifest,
    build_payload,
    get_provider,
    submit,
)
from .eligibility import ADMIN_WORDS, assess_group, assess_run
from .extract import build_evidence_pack
from .models import (
    DisclosureManifest,
    Eligibility,
    EligibilityStatus,
    EvidenceItem,
    EvidencePack,
    FileApprovalRecord,
    ManifestApproval,
    ProviderResult,
    SelectionProposal,
)
from .reader import ApprovedFileReader, ReadRefused
from .redact import redact
from .selection import ApprovalError, approve_files, propose_selection

__all__ = [
    "ADMIN_WORDS", "ApprovalError", "ApprovedFileReader", "DisclosureManifest", "DisclosureNotApproved",
    "DryRunProvider", "Eligibility", "EligibilityStatus", "EvidenceItem", "EvidencePack", "FileApprovalRecord",
    "ManifestApproval", "ProviderNotConfigured", "ProviderResult", "ReadRefused", "SelectionProposal",
    "approve_files", "approve_manifest", "assess_group", "assess_run", "build_evidence_pack", "build_manifest",
    "build_payload", "get_provider", "propose_selection", "redact", "submit",
]
