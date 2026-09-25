"""Data field census: which log fields are really present and filled in public flight logs.

The census answers one question before any counter or data model is designed: for each
field the maintenance counters would need, how many real logs carry it, and how many
carry real (non-zero) values. It stores booleans only, never positions or device ids.
"""
