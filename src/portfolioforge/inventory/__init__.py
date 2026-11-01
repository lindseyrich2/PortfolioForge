"""Read-only, metadata-only inventory of local academic and project files.

Typical use::

    from portfolioforge.inventory import scan_inventory
    inventory = scan_inventory(r"C:\\Users\\me\\PycharmProjects")

Full-computer scans are opt-in: build a plan with ``plan_full_computer()``,
review it, then call ``run_scan(plan, sink=..., allow_full_computer=True)``.
"""

from .config import FullScanOptions, InventoryConfig, ResourceLimits
from .formats import Category, FileFormat, lookup
from .models import (
    DirectoryRecord,
    EntryKind,
    FileRecord,
    Inventory,
    InventorySummary,
    OptionalLocation,
    PlanExclusion,
    RootOrigin,
    ScanMode,
    ScanPlan,
    ScanReport,
    ScanRoot,
    SkippedEntry,
    SkipReason,
)
from .platform import DriveInfo, DriveType, SystemInfo, detect_system
from .roots import ProtectedLocationError, plan_full_computer, plan_targeted
from .scanner import FullScanNotApproved, ScanProgress, run_scan, scan_inventory
from .sinks import JsonlSink, MemorySink, default_output_dir, load_inventory

__all__ = [
    "Category",
    "DirectoryRecord",
    "DriveInfo",
    "DriveType",
    "EntryKind",
    "FileFormat",
    "FileRecord",
    "FullScanNotApproved",
    "FullScanOptions",
    "Inventory",
    "InventoryConfig",
    "InventorySummary",
    "JsonlSink",
    "MemorySink",
    "OptionalLocation",
    "PlanExclusion",
    "ProtectedLocationError",
    "ResourceLimits",
    "RootOrigin",
    "ScanMode",
    "ScanPlan",
    "ScanProgress",
    "ScanReport",
    "ScanRoot",
    "SkipReason",
    "SkippedEntry",
    "SystemInfo",
    "default_output_dir",
    "detect_system",
    "load_inventory",
    "lookup",
    "plan_full_computer",
    "plan_targeted",
    "run_scan",
    "scan_inventory",
]
