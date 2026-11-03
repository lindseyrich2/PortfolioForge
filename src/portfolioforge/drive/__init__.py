"""Google Drive and Google Colab discovery. Colab notebooks live in Drive, so one integration covers both.

Read-only by construction: the client can only issue GET requests to the Drive files endpoints.
Discovery holds only a metadata permission, which cannot download anything; file contents need a
second, separate permission and a typed approval for the specific files (see :mod:`.hydrate`).
Results are ordinary inventories, so grouping, approvals, disclosure and publishing are unchanged.
"""
