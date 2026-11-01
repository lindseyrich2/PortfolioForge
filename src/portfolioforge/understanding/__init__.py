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
   Providers: :class:`DryRunProvider` (sends nothing) and :class:`AnthropicProvider`.
6. :func:`build_proposal` - verifies the model's structured answer locally and
   produces names, a summary and a draft README (with setup/run/test steps);
   :func:`edit_proposal` / :func:`accept_proposal` record your review. Nothing
   is ever published, and ownership is never inferred.
7. :func:`propose_build_files` / :func:`approve_build_files` - a separate approval for
   build configuration files. :func:`inspect_project` reads approved files, and the project's
   inspector works out how to build and run it, separating supported commands from guesses.
   With no inspector the result is "manual review needed". Sensitive data is flagged either
   way, and nothing is executed. :func:`apply_inspection` updates a proposal
   locally; :func:`build_augmented_pack` feeds a new manifest, which needs a new approval.
8. :func:`build_plan` / :func:`approve_plan` / :func:`execute_plan` - optional validation.
   The only code that runs a project: each plan needs its own single-use approval.

See docs/design/project-understanding-and-naming.md.
"""

from .analysis import (
    AnalysisProposal,
    InvalidModelOutput,
    ProposalError,
    accept_proposal,
    build_proposal,
    edit_proposal,
)
from .anthropic_provider import AnthropicProvider, ProviderRequestFailed
from .build_files import approve_build_files, propose_build_files
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
from .findings import apply_inspection, build_augmented_pack
from .inspection import inspect_project
from .inspectors import DEFAULT_REGISTRY, InspectorRegistry, ProjectInspector
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
from .validation import ValidationRefused, ValidationUnavailable, approve_plan, build_plan, execute_plan

__all__ = [
    "AnalysisProposal", "AnthropicProvider", "InvalidModelOutput", "ProposalError", "ProviderRequestFailed",
    "accept_proposal", "build_proposal", "edit_proposal",
    "ADMIN_WORDS", "ApprovalError", "ApprovedFileReader", "DisclosureManifest", "DisclosureNotApproved",
    "DryRunProvider", "Eligibility", "EligibilityStatus", "EvidenceItem", "EvidencePack", "FileApprovalRecord",
    "ManifestApproval", "ProviderNotConfigured", "ProviderResult", "ReadRefused", "SelectionProposal",
    "approve_files", "approve_manifest", "assess_group", "assess_run", "build_evidence_pack", "build_manifest",
    "build_payload", "get_provider", "propose_selection", "redact", "submit",
    "ValidationRefused", "ValidationUnavailable", "apply_inspection", "approve_build_files", "approve_plan",
    "build_augmented_pack", "build_plan", "execute_plan", "inspect_project", "propose_build_files",
    "DEFAULT_REGISTRY", "InspectorRegistry", "ProjectInspector",
]
