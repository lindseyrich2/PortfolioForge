"""GitHub discovery: repositories you can reach, as an ordinary inventory. Metadata only.

Colab can open a notebook straight from GitHub without saving it to Drive, so a Drive scan can't see it.
This adapter turns repository file listings into the same records a Drive scan builds, so grouping,
duplicate checks and the batch queue are reused. It never fetches file contents, and each repository
carries an attribution label that is a signal for your review, never proof of who wrote anything.
"""
